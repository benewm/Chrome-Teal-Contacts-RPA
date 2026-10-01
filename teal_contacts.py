"""Create Teal contacts from a spreadsheet.

    python teal_contacts.py contacts.xlsx            # show what a run would do
    python teal_contacts.py contacts.xlsx --reset    # forget progress, start over

The browser automation is added in later steps; for now this validates the
spreadsheet and reports which rows are pending, done or failed.
"""

from __future__ import annotations

import argparse
import sys

from teal_rpa.spreadsheet import SpreadsheetError, load_contacts
from teal_rpa.state import DONE, PENDING, RunState


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Create Teal contacts from a spreadsheet.")
    p.add_argument("spreadsheet", help="Path to the .xlsx or .csv file")
    p.add_argument("--sheet", help="Worksheet name (default: the first sheet)")
    p.add_argument("--reset", action="store_true",
                   help="Clear saved progress so every row is processed again")
    return p.parse_args(argv)


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

    valid = [c for c in sheet.contacts if c.is_valid]
    invalid = [c for c in sheet.contacts if not c.is_valid]
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
        missing = [f for f in ("email", "phone") if not getattr(c, f)]
        note = f"  (will ask for {' and '.join(missing)})" if missing else ""
        print(f"  - row {c.row_number} {c.name} | {c.title} @ {c.company}{note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
