"""Create Teal contacts from a spreadsheet.

    python teal_contacts.py contacts.xlsx               # review each contact before saving
    python teal_contacts.py contacts.xlsx --limit 1     # just the first pending contact
    python teal_contacts.py contacts.xlsx --dry-run     # fill forms, never save
    python teal_contacts.py contacts.xlsx --auto-save   # no review pause
    python teal_contacts.py contacts.xlsx --ask-missing # ask me for email/phone LinkedIn lacks
    python teal_contacts.py contacts.xlsx --check       # only validate the spreadsheet
    python teal_contacts.py contacts.xlsx --reset       # forget progress, start over

See README.md for setup.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import urlsplit

from teal_rpa.spreadsheet import Contact, Sheet, SpreadsheetError, load_contacts
from teal_rpa.state import DONE, PENDING, RunState


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Create Teal contacts from a spreadsheet.")
    p.add_argument("spreadsheet", help="Path to the .xlsx or .csv file")
    p.add_argument("--sheet", help="Worksheet name (default: the first sheet)")
    p.add_argument("--reset", action="store_true",
                   help="Clear saved progress so every row is processed again")
    p.add_argument("--check", action="store_true",
                   help="Only validate the spreadsheet and list pending contacts; don't open Chrome")
    p.add_argument("--limit", type=int, metavar="N",
                   help="Process at most N pending contacts this run")
    p.add_argument("--auto-save", action="store_true",
                   help="Click Save Contact without pausing for review")
    p.add_argument("--dry-run", action="store_true",
                   help="Do everything except Save (forms are filled, then cancelled)")
    p.add_argument("--no-linkedin", action="store_true",
                   help="Don't look up blank Email/Phone on the contact's LinkedIn Contact info")
    p.add_argument("--ask-missing", action="store_true",
                   help="Ask in the terminal for Email/Phone that are still blank")
    p.add_argument("--delay", type=float, default=4.0, metavar="SECONDS",
                   help="Pause between contacts (default: 4, randomised up to +50%%)")
    p.add_argument("--retries", type=int, default=2,
                   help="Retries when Teal's page or form doesn't load (default: 2)")
    p.add_argument("--timeout", type=float, default=20, metavar="SECONDS",
                   help="How long to wait for Teal's page and form (default: 20)")
    p.add_argument("--chrome-path", help="Path to chrome.exe if it isn't found automatically")
    p.add_argument("--profile-dir", type=Path,
                   help="Chrome profile folder for the tool (default: %%LOCALAPPDATA%%\\TealContactsRPA\\chrome-profile)")
    p.add_argument("--port", type=int, default=None, help="Chrome debugging port (default: 9333)")
    # For testing against a stand-in page.
    p.add_argument("--teal-url", help=argparse.SUPPRESS)
    p.add_argument("--linkedin-url", help=argparse.SUPPRESS)
    p.add_argument("--chrome-arg", action="append", default=[], help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        p.error("--limit must be at least 1")
    if args.auto_save and args.dry_run:
        p.error("--auto-save and --dry-run can't be used together")
    return args


def list_pending(sheet: Sheet, state: RunState, invalid: list[Contact],
                 linkedin: bool = True, ask: bool = False) -> None:
    valid = [c for c in sheet.contacts if c.is_valid]
    counts = state.counts(c.key for c in valid)
    where = f" (sheet '{sheet.sheet_name}')" if sheet.sheet_name else ""
    print(f"{sheet.path.name}{where}: {len(sheet.contacts)} contacts")
    print(f"  pending: {counts[PENDING]}   done: {counts[DONE]}   "
          f"failed: {counts['failed']}   skipped: {counts['skipped']}   "
          f"can't process: {len(invalid)}")
    for c in invalid:
        print(f"  ! row {c.row_number} {c.name}: {'; '.join(c.problems)}")
    for c in valid:
        if state.is_done(c.key):
            continue
        status = state.status(c.key)
        missing = [f for f in ("email", "phone")
                   if not getattr(c, f) and not state.recall(c.key, f)
                   and not state.recall(c.key, f"{f}_skipped")]
        notes = []
        if status != PENDING:
            notes.append(f"{status} last time: {state.recall(c.key, 'reason') or ''}".rstrip(": "))
        if missing:
            how = []
            if linkedin and not state.recall(c.key, "linkedin_checked"):
                how.append("look up on LinkedIn")
            if ask:
                how.append("ask")
            if how:
                notes.append(f"no {' or '.join(missing)}: will {', then '.join(how)}")
        note = f"  ({'; '.join(notes)})" if notes else ""
        print(f"  - row {c.row_number} {c.name} | {c.title} @ {c.company}{note}")


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

    state = RunState.for_spreadsheet(sheet.path)
    if args.reset:
        state.reset()
        print(f"Progress cleared ({state.path.name}).")
    for warning in sheet.warnings:
        print(f"Note: {warning}")

    invalid = [c for c in sheet.contacts if not c.is_valid]
    if args.check:
        list_pending(sheet, state, invalid, linkedin=not args.no_linkedin, ask=args.ask_missing)
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
    from teal_rpa.runner import Options, Prompter, Run, Summary, print_summary, say, setup_logging
    from teal_rpa.teal import TRACKER_URL, TealPage

    if not todo:
        print_summary(Summary(), invalid, already_done)
        print("Nothing to do.")
        return 0

    out_dir = sheet.path.parent
    log_path = setup_logging(out_dir / "logs")
    say(f"{len(todo)} contact(s) to process from {sheet.path.name}. Log: {log_path}")
    if args.dry_run:
        say("Dry run: forms will be filled but never saved.")

    tracker_url = args.teal_url or TRACKER_URL
    options = Options(auto_save=args.auto_save, dry_run=args.dry_run, limit=args.limit,
                      delay=args.delay, retries=args.retries,
                      linkedin=not args.no_linkedin, ask_missing=args.ask_missing)
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
                      teal=TealPage(page, tracker_url, timeout_ms=int(args.timeout * 1000)),
                      options=options, prompter=Prompter(),
                      failures_dir=out_dir / "failures",
                      linkedin=LinkedInPage(context, timeout_ms=int(args.timeout * 1000),
                                            origin=args.linkedin_url or LINKEDIN_ORIGIN))
            summary = run.run(todo)
    except browser.BrowserError as exc:
        say(f"Error: {exc}")
        return 2
    except KeyboardInterrupt:
        say("\nStopped (Ctrl+C).")
        return 130

    print_summary(summary, invalid, already_done)
    return 1 if summary.failed else 0


if __name__ == "__main__":
    sys.exit(main())
