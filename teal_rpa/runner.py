"""The batch run, in two passes.

Pass 1 (LinkedIn): for contacts missing an email or phone, read LinkedIn's
Contact info, ask when there's a choice or nothing at all, and write the
results into the spreadsheet. All the questions happen here.

Pass 2 (Teal, unattended): create the contact (or find the existing one),
record its Teal ID, set Relationship / Goal / Status / dates on its page,
then reload and check every field against the spreadsheet, fixing what
differs. Stops for you only when something still doesn't match.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from .compare import CHOICE_FIELDS, DATE_FIELDS, FORM_FIELDS, Mismatch, compare, expected_values
from .linkedin import ContactInfo, LinkedInError, LinkedInLoginRequired, LinkedInPage
from .spreadsheet import Contact, Sheet, SpreadsheetLockedError, ensure_columns, write_back
from .state import DONE, FAILED, NEEDS_ATTENTION, SKIPPED, RunState
from .teal import NotAnOption, NotLoggedIn, SaveUnconfirmed, TealError, TealPage

log = logging.getLogger("teal_rpa")

ASKABLE_FIELDS = {"email": "Email", "phone": "Phone"}
BASIC_FIELDS = ("first_name", "last_name", "title", "company", "url", "email", "phone",
                "location", "twitter")


@dataclass
class Options:
    dry_run: bool = False
    linkedin: bool = True        # pass 1: look up blank Email/Phone on LinkedIn
    review: bool = False         # pause before saving each new contact
    unattended: bool = False     # never pause in pass 2, even on mismatches
    linkedin_delay: float = 4.0  # pause between LinkedIn lookups
    teal_delay: float = 1.0      # pause between contacts in Teal
    retries: int = 2
    backoff: float = 2.0
    settle_ms: int = 1500        # wait for Teal's automatic save before re-checking


@dataclass
class Outcome:
    status: str  # DONE / NEEDS_ATTENTION / FAILED / SKIPPED, or "dry-run" / "quit"
    reason: str | None = None


@dataclass
class Summary:
    looked_up: int = 0
    found: list[str] = field(default_factory=list)   # "Ed Soo Hoo: email, phone"
    typed: list[str] = field(default_factory=list)
    nothing: list[str] = field(default_factory=list)
    done: list[Contact] = field(default_factory=list)
    attention: list[tuple[Contact, str]] = field(default_factory=list)
    failed: list[tuple[Contact, str]] = field(default_factory=list)
    skipped: list[tuple[Contact, str]] = field(default_factory=list)
    dry_run: list[tuple[Contact, str]] = field(default_factory=list)
    not_reached: int = 0


def say(message: str = "") -> None:
    """Print for the user and keep a copy in the log file."""
    print(message)
    if message.strip():
        log.info(message.strip())


class UserQuit(Exception):
    """You chose to stop (q), or the terminal closed."""


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
        while True:
            value = self.input(f"    {label} for {contact.name} [Enter to skip]: ").strip()
            if field == "email" and value and "@" not in value:
                print("      That doesn't look like an email address. Try again, or press Enter to skip.")
                continue
            return value

    def pick_one(self, contact: Contact, field: str, choices: list[str]) -> str:
        label = ASKABLE_FIELDS[field].lower()
        print(f"    LinkedIn lists {len(choices)} {label}s for {contact.name}:")
        for number, choice in enumerate(choices, start=1):
            print(f"      {number}) {choice}")
        while True:
            answer = self.input(f"    Which {label}? [Enter = 1, 0 = none]: ").strip()
            if not answer:
                return choices[0]
            if answer == "0":
                return ""
            if answer.isdigit() and 1 <= int(answer) <= len(choices):
                return choices[int(answer) - 1]

    def review(self, contact: Contact) -> str:
        """Returns 'save', 'skip' or 'quit'."""
        while True:
            answer = self.input(
                f"    Check the form in Chrome. Enter = Save Contact, s = skip "
                f"{contact.first_name or 'this one'}, q = quit: ").strip().lower()
            if answer in ("", "y", "yes"):
                return "save"
            if answer in ("s", "skip"):
                return "skip"
            if answer in ("q", "quit"):
                return "quit"

    def mismatch(self, contact: Contact, mismatches: list[Mismatch]) -> str:
        """Returns 'leave', 'retry' or 'recheck'."""
        print(f"    {contact.name} doesn't fully match the spreadsheet:")
        for m in mismatches:
            print(f"      - {m}")
        while True:
            answer = self.input(
                "    Enter = leave it marked 'needs attention', r = let the tool try again, "
                "c = I fixed it in Chrome, check again: ").strip().lower()
            if answer == "":
                return "leave"
            if answer in ("r", "retry"):
                return "retry"
            if answer in ("c", "check"):
                return "recheck"

    def wait_for_login(self, reason: str, site: str = "Teal") -> bool:
        print(f"\n  {reason}.")
        answer = self.input(
            f"  Sign in to {site} in the Chrome window the tool opened, then press Enter "
            "(q to quit): ").strip().lower()
        return answer not in ("q", "quit")

    def continue_to_teal(self, count: int, sheet_name: str) -> bool:
        print(f"\nLinkedIn pass finished. You can check {sheet_name} now "
              "(close it in Excel when you're done).")
        answer = self.input(f"Press Enter to load {count} contact(s) into Teal, or q to stop: ")
        return answer.strip().lower() not in ("q", "quit")


@dataclass
class Run:
    sheet: Sheet
    state: RunState
    teal: TealPage
    options: Options
    prompter: Prompter
    failures_dir: Path
    linkedin: LinkedInPage | None = None
    sleep: Callable[[float], None] = time.sleep
    summary: Summary = field(default_factory=Summary)

    def __post_init__(self):
        self.teal.debug_dir = self.failures_dir

    # -- shared helpers -------------------------------------------------------

    def contact_value(self, contact: Contact, field: str) -> str:
        """Spreadsheet value, else what an earlier run found/was told."""
        return getattr(contact, field) or self.state.recall(contact.key, field) or ""

    def save_to_sheet(self, contact: Contact, values: dict[str, str]) -> None:
        """Write to the spreadsheet; on a lock, keep values in the progress file."""
        if self.options.dry_run or not values:
            return
        self.state.remember(contact.key, **values)
        try:
            write_back(self.sheet, contact.row_number, values)
            log.info("Row %s: wrote %s to the spreadsheet", contact.row_number, ", ".join(values))
        except SpreadsheetLockedError as exc:
            say(f"    Note: {exc} (Kept in {self.state.path.name}; written on a later run.)")

    # -- pass 1: LinkedIn -----------------------------------------------------

    def needs_lookup(self, contact: Contact) -> bool:
        missing = [f for f in ASKABLE_FIELDS if not self.contact_value(contact, f)]
        return bool(missing) and not self.state.recall(contact.key, "enriched")

    def enrich_all(self, contacts: list[Contact]) -> bool:
        """Pass 1. Returns False if you chose to stop."""
        todo = [c for c in contacts if self.needs_lookup(c)]
        if not todo:
            return True
        say(f"\n--- LinkedIn: finding email/phone for {len(todo)} contact(s) ---")
        for index, contact in enumerate(todo, start=1):
            say(f"\n[{index}/{len(todo)}] Row {contact.row_number}: {contact.name}")
            try:
                looked = self.enrich(contact)
            except UserQuit:
                say("  Stopping.")
                return False
            if looked and index < len(todo) and self.options.linkedin_delay > 0:
                self.sleep(random.uniform(self.options.linkedin_delay, self.options.linkedin_delay * 1.5))
        return True

    def enrich(self, contact: Contact) -> bool:
        """Fill in this contact's blank email/phone. Returns True if LinkedIn was visited."""
        values = {f: self.contact_value(contact, f) for f in ASKABLE_FIELDS}
        info: ContactInfo | None = None
        looked = False
        if self.linkedin and self.options.linkedin:
            info = self.lookup_linkedin(contact)
            looked = True
            self.summary.looked_up += 1

        found: dict[str, str] = {}
        if info is not None:
            for field, choices in (("email", info.emails), ("phone", info.phones)):
                if values[field] or not choices:
                    continue
                chosen = choices[0] if len(choices) == 1 else \
                    self.prompter.pick_one(contact, field, choices)
                if chosen:
                    values[field] = found[field] = chosen
            if found:
                say("    From LinkedIn: " + ", ".join(f"{ASKABLE_FIELDS[f]} {v}" for f, v in found.items()))
                self.summary.found.append(f"{contact.name}: {', '.join(found)}")
            elif not (info.emails or info.phones):
                say(f"    LinkedIn: {info.note or 'nothing found'}")
                self.save_linkedin_evidence(contact, info)

        typed: dict[str, str] = {}
        if not values["email"] and not values["phone"]:
            say(f"    No email or phone for {contact.name}.")
            for field in ASKABLE_FIELDS:
                value = self.prompter.ask_field(contact, field)
                if value:
                    values[field] = typed[field] = value
            if typed:
                self.summary.typed.append(f"{contact.name}: {', '.join(typed)}")
            else:
                self.summary.nothing.append(contact.name)

        self.save_to_sheet(contact, {**found, **typed})
        # If LinkedIn couldn't be read and nothing was typed, try LinkedIn again next run.
        if not self.options.dry_run and (info is not None or typed or not looked):
            self.state.remember(contact.key, enriched=True)
        return looked

    def lookup_linkedin(self, contact: Contact) -> ContactInfo | None:
        """LinkedIn Contact info, or None if it couldn't be read."""
        for _ in range(2):
            try:
                return self.linkedin.lookup(contact.url)
            except LinkedInLoginRequired as exc:
                if not self.prompter.wait_for_login(str(exc), site="LinkedIn"):
                    raise UserQuit() from exc
            except LinkedInError as exc:
                say(f"    LinkedIn: {exc}.")
                self.save_linkedin_evidence(contact, ContactInfo(note=str(exc)))
                return None
        return None

    def save_linkedin_evidence(self, contact: Contact, info: ContactInfo) -> None:
        """Keep what LinkedIn showed when nothing was found, to check for misses."""
        try:
            self.failures_dir.mkdir(parents=True, exist_ok=True)
            base = self.failures_dir / f"linkedin-row-{contact.row_number}"
            base.with_suffix(".txt").write_text(
                f"{contact.name}\n{contact.url}\n{info.note or ''}\n\n"
                f"--- Contact info box text ---\n{info.dialog_text or '(none)'}\n",
                encoding="utf-8")
            # Chrome only draws the tab in front, so switch to it briefly.
            self.linkedin.page.bring_to_front()
            self.linkedin.page.screenshot(path=str(base.with_suffix(".png")), timeout=10_000)
            log.info("Row %s: LinkedIn evidence saved to %s.*", contact.row_number, base)
        except Exception as exc:  # evidence is best effort
            log.warning("Row %s: couldn't save LinkedIn evidence: %s", contact.row_number, exc)
        finally:
            self.teal.page.bring_to_front()

    # -- pass 2: Teal ---------------------------------------------------------

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

    def load_all(self, contacts: list[Contact]) -> None:
        """Pass 2."""
        self.teal.page.bring_to_front()
        try:
            if not self.ensure_logged_in():
                self.summary.not_reached += len(contacts)
                return
        except UserQuit:
            self.summary.not_reached += len(contacts)
            return
        if not self.options.dry_run:
            try:
                added = ensure_columns(self.sheet, ["teal_id", "rpa_status", "rpa_notes"])
                if added:
                    say(f"Added column(s) {', '.join(added)} to {self.sheet.path.name}.")
            except SpreadsheetLockedError as exc:
                say(f"Note: {exc} Results are kept in {self.state.path.name} for now.")

        say(f"\n--- Teal: {len(contacts)} contact(s) ---")
        total = len(contacts)
        for index, contact in enumerate(contacts, start=1):
            say(f"\n[{index}/{total}] Row {contact.row_number}: {contact.name}"
                f" - {contact.title} @ {contact.company}")
            for warning in contact.warnings:
                say(f"    Note: {warning}")
            try:
                try:
                    outcome = self.process_contact(contact)
                except NotLoggedIn as exc:
                    if not self.prompter.wait_for_login(str(exc)):
                        raise UserQuit() from exc
                    outcome = self.process_contact(contact)
            except UserQuit:
                outcome = Outcome("quit")
            except KeyboardInterrupt:
                say("\nStopped (Ctrl+C).")
                self.summary.not_reached += total - index + 1
                return

            if outcome.status == "quit":
                say("  Stopping; this contact stays pending.")
                self.summary.not_reached += total - index + 1
                return
            self.record(contact, outcome)
            if index < total and self.options.teal_delay > 0:
                self.sleep(random.uniform(self.options.teal_delay, self.options.teal_delay * 1.5))

    def record(self, contact: Contact, outcome: Outcome) -> None:
        reason = outcome.reason or ""
        if outcome.status == "dry-run":
            say(f"  Dry run: {reason}")
            self.summary.dry_run.append((contact, reason))
            return
        self.state.mark(contact.key, outcome.status, row_number=contact.row_number,
                        name=contact.name, reason=outcome.reason)
        self.save_to_sheet(contact, {"rpa_status": outcome.status, "rpa_notes": reason})
        if outcome.status == DONE:
            say("  Done - everything matches the spreadsheet.")
            self.summary.done.append(contact)
        elif outcome.status == NEEDS_ATTENTION:
            say(f"  NEEDS ATTENTION: {reason}")
            self.summary.attention.append((contact, reason))
        elif outcome.status == FAILED:
            say(f"  FAILED: {reason}")
            self.summary.failed.append((contact, reason))
        else:
            say(f"  Skipped ({reason}).")
            self.summary.skipped.append((contact, reason))

    def process_contact(self, contact: Contact) -> Outcome:
        values = {f: self.contact_value(contact, f) if f in ASKABLE_FIELDS else getattr(contact, f)
                  for f in BASIC_FIELDS}
        expected = expected_values(contact, values["email"], values["phone"])

        teal_id = contact.teal_id or self.state.recall(contact.key, "teal_id") or ""
        created = False
        if not teal_id:
            result = self.with_retries(contact, lambda: self.teal.find_existing(
                contact.first_name, contact.last_name, contact.url))
            if isinstance(result, Outcome):
                return result
            if result:
                teal_id = result
                say("    Already in Teal - updating it.")
        if not teal_id:
            result = self.create(contact, values)
            if isinstance(result, Outcome):
                return result
            teal_id, created = result, True
            say(f"    Created in Teal (ID {teal_id}).")
        if not self.options.dry_run:
            self.save_to_sheet(contact, {"teal_id": teal_id})

        result = self.with_retries(contact, lambda: self.sync_record(contact, teal_id, values, expected))
        if isinstance(result, Outcome):
            return result
        mismatches, notes = result
        if self.options.dry_run:
            changes = "; ".join(str(m) for m in mismatches) or "nothing to change"
            return Outcome("dry-run", f"existing contact - would fix: {changes}")

        while mismatches and not self.options.unattended:
            choice = self.prompter.mismatch(contact, mismatches)
            if choice == "leave":
                break
            result = self.with_retries(contact, lambda: self.sync_record(
                contact, teal_id, values, expected, fix=choice == "retry"))
            if isinstance(result, Outcome):
                return result
            mismatches, more = result
            notes += more

        if mismatches:
            reason = "; ".join([str(m) for m in mismatches] + notes)
            self._screenshot(contact)
            return Outcome(NEEDS_ATTENTION, reason)
        return Outcome(DONE, "created" if created else "updated existing contact")

    def create(self, contact: Contact, values: dict[str, str]):
        """New contact via the Add form. Returns its id, or an Outcome."""
        attempts = []

        def fill():
            # On a retry, reload so nothing from the failed attempt lingers.
            self.teal.open_tracker(reload=bool(attempts))
            attempts.append(1)
            frame = self.teal.open_form()
            for note in self.teal.fill(frame, values):
                say(f"    Note: {note}")
            return frame

        frame = self.with_retries(contact, fill)
        if isinstance(frame, Outcome):
            return frame
        if self.options.dry_run:
            self.teal.cancel(frame)
            return Outcome("dry-run", "new contact - form filled, then cancelled")
        if self.options.review:
            choice = self.prompter.review(contact)
            if choice != "save":
                self.teal.cancel(frame)
                return Outcome("quit") if choice == "quit" else Outcome(SKIPPED, "skipped at review")
        try:
            teal_id = self.teal.create(frame, contact.first_name, contact.last_name, contact.url)
        except SaveUnconfirmed as exc:
            return self._fail(contact, f"{exc}. It may have been saved - check Teal before re-running.")
        except TealError as exc:
            return self._fail(contact, f"Saved, but then: {exc}. Check Teal before re-running.")
        if not teal_id:
            return self._fail(contact, "Saved, but couldn't find the new contact in Teal's list "
                                       "to get its ID. Check Teal, then put its ID in the "
                                       "teal_contact_id column (from the address bar) and re-run.")
        self.state.remember(contact.key, teal_id=teal_id)  # before anything else can fail
        return teal_id

    def sync_record(self, contact: Contact, teal_id: str, values: dict[str, str],
                    expected: dict[str, str], fix: bool = True) -> tuple[list[Mismatch], list[str]]:
        """Make the contact's page match the spreadsheet: read, fix, reload,
        check. Returns (remaining mismatches, notes)."""
        self.teal.open_record(teal_id, reload=True)
        mismatches = compare(expected, self.teal.read_record())
        notes: list[str] = []
        if self.options.dry_run or not fix:
            return mismatches, notes
        for _ in range(2):  # set everything, then one more go at what didn't stick
            if not mismatches:
                break
            notes = self.apply_fixes(mismatches, values, expected)
            self.teal.settle(self.options.settle_ms)
            self.teal.open_record(teal_id, reload=True)
            mismatches = compare(expected, self.teal.read_record())
        return mismatches, notes

    def apply_fixes(self, mismatches: list[Mismatch], values: dict[str, str],
                    expected: dict[str, str]) -> list[str]:
        notes: list[str] = []
        fields = {m.field for m in mismatches}
        for name in CHOICE_FIELDS:
            if name in fields:
                try:
                    self.teal.set_choice(name, expected[name])
                except NotAnOption as exc:
                    notes.append(str(exc))
        for name in DATE_FIELDS:
            if name in fields:
                self.teal.set_date(name, expected[name])
        if fields & set(FORM_FIELDS):
            frame = self.teal.open_edit_form()
            try:
                self.teal.fill(frame, values)
                self.teal.save(frame)
            except SaveUnconfirmed as exc:
                self.teal.cancel(frame)
                notes.append(f"Edit form: {exc}")
        return notes

    def with_retries(self, contact: Contact, action: Callable):
        """Run a step that's safe to repeat; on repeated failure return an Outcome."""
        attempts = self.options.retries + 1
        for attempt in range(1, attempts + 1):
            try:
                return action()
            except NotLoggedIn:
                raise
            except TealError as exc:
                log.warning("Row %s attempt %s/%s: %s", contact.row_number, attempt, attempts, exc)
                if attempt == attempts:
                    return self._fail(contact, str(exc))
                wait = self.options.backoff * 2 ** (attempt - 1)
                say(f"    {exc} - retrying in {wait:.0f}s")
                self.sleep(wait)

    def _screenshot(self, contact: Contact) -> Path | None:
        return self.teal.screenshot(self.failures_dir / f"row-{contact.row_number}.png")

    def _fail(self, contact: Contact, reason: str) -> Outcome:
        shot = self._screenshot(contact)
        return Outcome(FAILED, f"{reason} [screenshot: {shot}]" if shot else reason)


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
    if summary.looked_up or summary.typed or summary.nothing:
        say(f"LinkedIn: checked {summary.looked_up}; found details for {len(summary.found)}; "
            f"you typed details for {len(summary.typed)}; nothing for {len(summary.nothing)}")
        for line in summary.nothing:
            say(f"  - no email or phone: {line}")
    if summary.dry_run:
        say(f"Dry run: {len(summary.dry_run)} contact(s) walked through, nothing saved.")
        for contact, reason in summary.dry_run:
            say(f"  - row {contact.row_number} {contact.name}: {reason}")
    say(f"Done:            {len(summary.done)}")
    say(f"Needs attention: {len(summary.attention)}")
    for contact, reason in summary.attention:
        say(f"  - row {contact.row_number} {contact.name}: {reason}")
    say(f"Failed:          {len(summary.failed)}")
    for contact, reason in summary.failed:
        say(f"  - row {contact.row_number} {contact.name}: {reason}")
    skipped = summary.skipped + [(c, "; ".join(c.problems)) for c in invalid]
    say(f"Skipped:         {len(skipped)}")
    for contact, reason in skipped:
        say(f"  - row {contact.row_number} {contact.name}: {reason}")
    if already_done:
        say(f"Already done on earlier runs: {already_done}")
    if summary.not_reached:
        say(f"Not reached this run: {summary.not_reached} (still pending)")
