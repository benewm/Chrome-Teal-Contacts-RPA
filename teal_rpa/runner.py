"""The batch run: ask for missing details, fill Teal's form, track results."""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from .spreadsheet import Contact, Sheet, SpreadsheetLockedError, write_back
from .state import DONE, FAILED, SKIPPED, RunState
from .teal import NotLoggedIn, SaveUnconfirmed, TealError, TealPage

log = logging.getLogger("teal_rpa")

ASKABLE_FIELDS = {"email": "Email", "phone": "Phone"}


@dataclass
class Options:
    auto_save: bool = False
    dry_run: bool = False
    limit: int | None = None
    delay: float = 2.0
    retries: int = 2
    backoff: float = 2.0


@dataclass
class Outcome:
    status: str  # DONE / FAILED / SKIPPED, or "dry-run" / "quit" (not recorded)
    reason: str | None = None


@dataclass
class Summary:
    done: list[Contact] = field(default_factory=list)
    failed: list[tuple[Contact, str]] = field(default_factory=list)
    skipped: list[tuple[Contact, str]] = field(default_factory=list)
    dry_run: list[Contact] = field(default_factory=list)
    not_reached: int = 0


def say(message: str = "") -> None:
    """Print for the user and keep a copy in the log file."""
    print(message)
    if message.strip():
        log.info(message.strip())


class UserQuit(Exception):
    """The terminal closed (no more input); stop like 'q'."""


class Prompter:
    """Terminal questions. input_fn is swappable so tests can script answers."""

    def __init__(self, input_fn: Callable[[str], str] = input):
        self._input = input_fn

    def input(self, question: str) -> str:
        try:
            return self._input(question)
        except EOFError:
            raise UserQuit() from None

    def ask_field(self, contact: Contact, field: str) -> str:
        label = ASKABLE_FIELDS[field]
        context = " @ ".join(p for p in (contact.title, contact.company) if p)
        while True:
            value = self.input(
                f"  {label} for {contact.name}{f' ({context})' if context else ''} "
                f"[Enter to skip]: "
            ).strip()
            if field == "email" and value and "@" not in value:
                print("    That doesn't look like an email address. Try again, or press Enter to skip.")
                continue
            return value

    def review(self, contact: Contact) -> str:
        """Returns 'save', 'skip' or 'quit'."""
        while True:
            answer = self.input(
                f"  Check the form in Chrome. Enter = Save Contact, s = skip {contact.first_name or 'this one'}, "
                "q = quit: "
            ).strip().lower()
            if answer in ("", "y", "yes"):
                return "save"
            if answer in ("s", "skip"):
                return "skip"
            if answer in ("q", "quit"):
                return "quit"

    def wait_for_login(self, reason: str) -> bool:
        print(f"\n  {reason}.")
        answer = self.input(
            "  Sign in to Teal in the Chrome window the tool opened, then press Enter "
            "(q to quit): "
        ).strip().lower()
        return answer not in ("q", "quit")


@dataclass
class Run:
    sheet: Sheet
    state: RunState
    teal: TealPage
    options: Options
    prompter: Prompter
    failures_dir: Path
    sleep: Callable[[float], None] = time.sleep

    # -- one contact ----------------------------------------------------------

    def fill_missing(self, contact: Contact) -> dict[str, str]:
        """Return the values to type into Teal, asking for blank Email/Phone."""
        values = {
            "first_name": contact.first_name, "last_name": contact.last_name,
            "title": contact.title, "company": contact.company, "url": contact.url,
            "email": contact.email, "phone": contact.phone,
            "location": contact.location, "twitter": contact.twitter,
        }
        typed: dict[str, str] = {}
        for field in ASKABLE_FIELDS:
            if values[field]:
                continue
            # Answered on an earlier run (e.g. the row failed afterwards)?
            remembered = self.state.recall(contact.key, field)
            if remembered:
                values[field] = remembered
                continue
            if self.state.recall(contact.key, f"{field}_skipped"):
                continue
            value = self.prompter.ask_field(contact, field)
            values[field] = value
            if self.options.dry_run:
                continue
            if value:
                typed[field] = value
                self.state.remember(contact.key, **{field: value})
            else:
                self.state.remember(contact.key, **{f"{field}_skipped": True})

        if typed:
            try:
                write_back(self.sheet, contact.row_number, typed)
                log.info("Row %s: wrote %s to the spreadsheet", contact.row_number, ", ".join(typed))
            except SpreadsheetLockedError as exc:
                say(f"    Note: {exc} (Kept in {self.state.path.name}; it'll still be used.)")
        return values

    def process_row(self, contact: Contact) -> Outcome:
        values = self.fill_missing(contact)

        frame = None
        attempts = self.options.retries + 1
        for attempt in range(1, attempts + 1):
            try:
                self.teal.open_tracker(reload=attempt > 1)
                frame = self.teal.open_form()
                for note in self.teal.fill(frame, values):
                    say(f"    Note: {note}")
                break
            except NotLoggedIn:
                raise
            except TealError as exc:
                log.warning("Row %s attempt %s/%s: %s", contact.row_number, attempt, attempts, exc)
                if frame is not None:
                    self.teal.cancel(frame)
                    frame = None
                if attempt == attempts:
                    return self._fail(contact, str(exc))
                wait = self.options.backoff * 2 ** (attempt - 1)
                say(f"    {exc} - retrying in {wait:.0f}s")
                self.sleep(wait)

        if self.options.dry_run:
            self.teal.cancel(frame)
            return Outcome("dry-run")

        if not self.options.auto_save:
            choice = self.prompter.review(contact)
            if choice != "save":
                self.teal.cancel(frame)
                return Outcome("quit") if choice == "quit" else Outcome(SKIPPED, "skipped at review")
            if not self.teal.form_is_open(frame):
                return self._fail(contact, "The form was closed in Chrome before the tool clicked "
                                           "Save. Check Teal for this contact.")

        try:
            self.teal.save(frame)
        except TealError as exc:
            self.teal.cancel(frame)
            return self._fail(contact, str(exc))
        except SaveUnconfirmed as exc:
            return self._fail(contact, f"{exc}. It may have been saved - check Teal before re-running.")
        return Outcome(DONE)

    def _fail(self, contact: Contact, reason: str) -> Outcome:
        shot = self.teal.screenshot(self.failures_dir / f"row-{contact.row_number}.png")
        if shot:
            reason = f"{reason} [screenshot: {shot}]"
        return Outcome(FAILED, reason)

    # -- the batch ------------------------------------------------------------

    def ensure_logged_in(self) -> bool:
        while True:
            try:
                self.teal.open_tracker()
                return True
            except NotLoggedIn as exc:
                if not self.prompter.wait_for_login(str(exc)):
                    return False
            except TealError as exc:
                say(f"  Teal's contact tracker didn't load: {exc}")
                if not self.prompter.wait_for_login("Teal may need you to sign in"):
                    return False

    def run(self, contacts: list[Contact]) -> Summary:
        summary = Summary()
        try:
            logged_in = self.ensure_logged_in()
        except UserQuit:
            logged_in = False
        if not logged_in:
            summary.not_reached = len(contacts)
            return summary

        total = len(contacts)
        for index, contact in enumerate(contacts, start=1):
            say(f"\n[{index}/{total}] Row {contact.row_number}: {contact.name}"
                f" - {contact.title} @ {contact.company}")
            try:
                try:
                    outcome = self.process_row(contact)
                except NotLoggedIn as exc:
                    if not self.prompter.wait_for_login(str(exc)):
                        summary.not_reached = total - index + 1
                        break
                    outcome = self.process_row(contact)
            except UserQuit:
                outcome = Outcome("quit")
            except KeyboardInterrupt:
                say("\nStopped (Ctrl+C).")
                summary.not_reached = total - index + 1
                break

            if outcome.status == "quit":
                say("  Quitting; this contact stays pending.")
                summary.not_reached = total - index + 1
                break
            if outcome.status == "dry-run":
                say("  Dry run: form filled, then cancelled (nothing saved).")
                summary.dry_run.append(contact)
            else:
                self.state.mark(contact.key, outcome.status, row_number=contact.row_number,
                                name=contact.name, reason=outcome.reason)
                if outcome.status == DONE:
                    say("  Saved.")
                    summary.done.append(contact)
                elif outcome.status == FAILED:
                    say(f"  FAILED: {outcome.reason}")
                    summary.failed.append((contact, outcome.reason or ""))
                else:
                    say(f"  Skipped ({outcome.reason}).")
                    summary.skipped.append((contact, outcome.reason or ""))

            if index < total and self.options.delay > 0:
                self.sleep(random.uniform(self.options.delay, self.options.delay * 1.5))
        return summary


def setup_logging(log_dir: Path) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"teal_contacts-{datetime.now():%Y%m%d-%H%M%S}.log"
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    return path


def print_summary(summary: Summary, invalid: list[Contact], already_done: int) -> None:
    say("\n" + "=" * 60)
    if summary.dry_run:
        say(f"Dry run: {len(summary.dry_run)} form(s) filled and cancelled.")
    say(f"Succeeded: {len(summary.done)}")
    say(f"Failed:    {len(summary.failed)}")
    for contact, reason in summary.failed:
        say(f"  - row {contact.row_number} {contact.name}: {reason}")
    skipped = summary.skipped + [(c, "; ".join(c.problems)) for c in invalid]
    say(f"Skipped:   {len(skipped)}")
    for contact, reason in skipped:
        say(f"  - row {contact.row_number} {contact.name}: {reason}")
    if already_done:
        say(f"Already done on earlier runs: {already_done}")
    if summary.not_reached:
        say(f"Not reached this run: {summary.not_reached} (still pending)")
