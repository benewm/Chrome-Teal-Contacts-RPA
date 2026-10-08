"""Load contacts from a spreadsheet into Teal, in two passes.

    python teal_contacts.py contacts.xlsx                  # LinkedIn pass, then Teal pass
    python teal_contacts.py contacts.xlsx --check          # only validate; no browser
    python teal_contacts.py contacts.xlsx --limit 1        # just the first pending contact
    python teal_contacts.py contacts.xlsx --dry-run        # walk through, never save
    python teal_contacts.py contacts.xlsx --linkedin-only  # just find emails/phones
    python teal_contacts.py contacts.xlsx --teal-only      # skip LinkedIn
    python teal_contacts.py contacts.xlsx --reset          # forget progress, start over

See README.md for setup.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import urlsplit

from teal_rpa.spreadsheet import CHOICE_FIELDS, Contact, Sheet, SpreadsheetError, load_contacts
from teal_rpa.state import DONE, PENDING, RunState


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Load contacts from a spreadsheet into Teal.")
    p.add_argument("spreadsheet", help="Path to the .xlsx or .csv file")
    p.add_argument("--sheet", help="Worksheet name (default: the first sheet)")
    p.add_argument("--reset", action="store_true",
                   help="Clear saved progress so every row is processed again")
    p.add_argument("--check", action="store_true",
                   help="Only validate the spreadsheet and list pending contacts; don't open Chrome")
    p.add_argument("--limit", type=int, metavar="N",
                   help="Process at most N pending contacts this run")
    p.add_argument("--dry-run", action="store_true",
                   help="Walk through everything without saving anything in Teal or the spreadsheet")
    passes = p.add_mutually_exclusive_group()
    passes.add_argument("--linkedin-only", action="store_true",
                        help="Only the LinkedIn pass (find emails/phones, write them to the sheet)")
    passes.add_argument("--teal-only", action="store_true",
                        help="Only the Teal pass (don't visit LinkedIn)")
    p.add_argument("--yes", action="store_true",
                   help="Go straight from the LinkedIn pass to the Teal pass without pausing")
    p.add_argument("--unattended", action="store_true",
                   help="Never pause in the Teal pass; mismatches are just marked 'needs attention'")
    p.add_argument("--review", action="store_true",
                   help="Pause before saving each new contact so you can look at the form")
    p.add_argument("--delay", type=float, default=4.0, metavar="SECONDS",
                   help="Pause between LinkedIn lookups (default: 4, randomised up to +50%%)")
    p.add_argument("--retries", type=int, default=2,
                   help="Retries when Teal's page or form doesn't load (default: 2)")
    p.add_argument("--timeout", type=float, default=20, metavar="SECONDS",
                   help="How long to wait for Teal's and LinkedIn's pages (default: 20)")
    p.add_argument("--chrome-path", help="Path to chrome.exe if it isn't found automatically")
    p.add_argument("--profile-dir", type=Path,
                   help="Chrome profile folder for the tool (default: %%LOCALAPPDATA%%\\TealContactsRPA\\chrome-profile)")
    p.add_argument("--port", type=int, default=None, help="Chrome debugging port (default: 9333)")
    # For testing against stand-in pages.
    p.add_argument("--teal-url", help=argparse.SUPPRESS)
    p.add_argument("--linkedin-url", help=argparse.SUPPRESS)
    p.add_argument("--chrome-arg", action="append", default=[], help=argparse.SUPPRESS)
    p.add_argument("--settle-ms", type=int, default=1500, help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        p.error("--limit must be at least 1")
    return args


def check_choices(sheet: Sheet) -> None:
    """Warn up front about dropdown values Teal doesn't offer."""
    from teal_rpa.teal import CHOICE_FIELDS as PAGE_CHOICES
    from teal_rpa.teal import CHOICE_OPTIONS, pick_option

    for contact in sheet.contacts:
        for field in CHOICE_FIELDS:
            value = getattr(contact, field)
            if value and not pick_option(value, CHOICE_OPTIONS[field]):
                contact.warnings.append(
                    f"{PAGE_CHOICES[field][1]} {value!r} isn't one of Teal's options "
                    f"({', '.join(CHOICE_OPTIONS[field])})")


def list_pending(sheet: Sheet, state: RunState, invalid: list[Contact], linkedin: bool) -> None:
    valid = [c for c in sheet.contacts if c.is_valid]
    counts = state.counts(c.key for c in valid)
    where = f" (sheet '{sheet.sheet_name}')" if sheet.sheet_name else ""
    print(f"{sheet.path.name}{where}: {len(sheet.contacts)} contacts")
    print("  " + "   ".join(f"{name}: {count}" for name, count in counts.items())
          + f"   can't process: {len(invalid)}")
    for c in invalid:
        print(f"  ! row {c.row_number} {c.name}: {'; '.join(c.problems)}")
    for c in valid:
        if state.is_done(c.key):
            continue
        notes = []
        status = state.status(c.key)
        if status != PENDING:
            notes.append(f"{status} last time: {state.recall(c.key, 'reason') or ''}".rstrip(": "))
        missing = [f for f in ("email", "phone") if not getattr(c, f) and not state.recall(c.key, f)]
        if missing and not state.recall(c.key, "enriched"):
            notes.append(f"no {' or '.join(missing)}: will "
                         + ("look up on LinkedIn" if linkedin else "ask"))
        if c.teal_id or state.recall(c.key, "teal_id"):
            notes.append("already in Teal")
        settings = ", ".join(f"{f}: {getattr(c, f)}" for f in
                             ("relationship", "goal", "status", "follow_up") if getattr(c, f))
        print(f"  - row {c.row_number} {c.name} | {c.title} @ {c.company}"
              + (f"\n      {settings}" if settings else "")
              + "".join(f"\n      ({n})" for n in notes)
              + "".join(f"\n      Note: {w}" for w in c.warnings))


def pick_page(context, tracker_url: str):
    """Reuse the tracker tab (or the tab Chrome opened at start-up) if there is one."""
    host = urlsplit(tracker_url).netloc
    pages = context.pages
    for match in (lambda p: p.url.startswith(tracker_url),
                  lambda p: urlsplit(p.url).netloc == host):
        page = next((p for p in pages if match(p)), None)
        if page:
            return page
    return context.new_page()


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        sheet = load_contacts(args.spreadsheet, args.sheet)
    except SpreadsheetError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    check_choices(sheet)

    state = RunState.for_spreadsheet(sheet.path)
    if args.reset:
        state.reset()
        print(f"Progress cleared ({state.path.name}).")
    for warning in sheet.warnings:
        print(f"Note: {warning}")

    invalid = [c for c in sheet.contacts if not c.is_valid]
    if args.check:
        list_pending(sheet, state, invalid, linkedin=not args.teal_only)
        return 0

    valid = [c for c in sheet.contacts if c.is_valid]
    todo = [c for c in valid if not state.is_done(c.key)]
    already_done = len(valid) - len(todo)
    if args.limit:
        todo = todo[: args.limit]

    # Imported here so --check works without Playwright installed.
    from playwright.sync_api import sync_playwright

    from teal_rpa import browser
    from teal_rpa.linkedin import LINKEDIN_ORIGIN, LinkedInPage
    from teal_rpa.runner import Options, Prompter, Run, Summary, UserQuit, print_summary, say, setup_logging
    from teal_rpa.teal import TRACKER_URL, TealPage

    if not todo:
        print_summary(Summary(), invalid, already_done)
        print("Nothing to do.")
        return 0

    out_dir = sheet.path.parent
    log_path = setup_logging(out_dir / "logs")
    say(f"{len(todo)} contact(s) to process from {sheet.path.name}. Log: {log_path}")
    if args.dry_run:
        say("Dry run: nothing will be saved in Teal or the spreadsheet.")

    tracker_url = args.teal_url or TRACKER_URL
    timeout_ms = int(args.timeout * 1000)
    options = Options(dry_run=args.dry_run, linkedin=not args.teal_only, review=args.review,
                      unattended=args.unattended, linkedin_delay=args.delay,
                      retries=args.retries, settle_ms=args.settle_ms)
    prompter = Prompter()
    try:
        with sync_playwright() as pw:
            endpoint = browser.ensure_chrome(
                chrome_path=args.chrome_path,
                profile_dir=args.profile_dir or browser.default_profile_dir(),
                port=args.port or browser.DEFAULT_PORT,
                start_url=tracker_url,
                extra_args=args.chrome_arg,
            )
            _, context = browser.connect(pw, endpoint)
            page = pick_page(context, tracker_url)
            page.bring_to_front()
            run = Run(sheet=sheet, state=state,
                      teal=TealPage(page, tracker_url, timeout_ms=timeout_ms),
                      options=options, prompter=prompter,
                      failures_dir=out_dir / "failures",
                      linkedin=LinkedInPage(context, timeout_ms=timeout_ms,
                                            origin=args.linkedin_url or LINKEDIN_ORIGIN))

            go_on = True
            if not args.teal_only:
                asked_before = run.summary.looked_up
                go_on = run.enrich_all(todo)
                lookups_done = run.summary.looked_up > asked_before
                if go_on and not args.linkedin_only and lookups_done and not args.yes:
                    try:
                        go_on = prompter.continue_to_teal(len(todo), sheet.path.name)
                    except UserQuit:
                        go_on = False
                if go_on and not args.linkedin_only and lookups_done:
                    # Pick up anything you changed in the spreadsheet meanwhile.
                    keys = {c.key for c in todo}
                    run.sheet = sheet = load_contacts(sheet.path, args.sheet)
                    check_choices(sheet)
                    todo = [c for c in sheet.contacts if c.is_valid and c.key in keys]
            if args.linkedin_only:
                say("\nLinkedIn pass finished (--linkedin-only).")
            elif go_on:
                run.load_all(todo)
            else:
                run.summary.not_reached += len(todo)
            summary = run.summary
    except browser.BrowserError as exc:
        say(f"Error: {exc}")
        return 2
    except KeyboardInterrupt:
        say("\nStopped (Ctrl+C).")
        return 130

    print_summary(summary, invalid, already_done)
    return 1 if summary.failed or summary.attention else 0


if __name__ == "__main__":
    sys.exit(main())
