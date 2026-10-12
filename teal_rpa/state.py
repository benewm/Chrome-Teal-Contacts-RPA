"""Per-spreadsheet run tracking (the "progress file").

Kept on this PC, outside OneDrive/Dropbox, in
%LOCALAPPDATA%\\TealContactsRPA\\progress\\<sheet name>-<id>.status.json: it's
rewritten after every step, and a syncing folder locks files it's uploading.

Entries are keyed by the normalized LinkedIn URL (Contact.key), so sorting or
inserting rows in the spreadsheet doesn't confuse which contacts are done.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("teal_rpa")

PENDING = "pending"
DONE = "done"
FAILED = "failed"
SKIPPED = "skipped"
NEEDS_ATTENTION = "needs attention"  # in Teal, but some fields don't match the sheet
STATUSES = (PENDING, DONE, FAILED, SKIPPED, NEEDS_ATTENTION)

SCHEMA_VERSION = 1


def app_data_dir() -> Path:
    """The tool's own folder on this PC (TEAL_RPA_DATA_DIR overrides it)."""
    if os.environ.get("TEAL_RPA_DATA_DIR"):
        return Path(os.environ["TEAL_RPA_DATA_DIR"])
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path.home() / ".local" / "share"
    return base / "TealContactsRPA"


def status_path_for(spreadsheet: str | Path) -> Path:
    spreadsheet = Path(spreadsheet).resolve()
    ident = hashlib.sha1(str(spreadsheet).casefold().encode("utf-8")).hexdigest()[:8]
    return app_data_dir() / "progress" / f"{spreadsheet.name}-{ident}.status.json"


def old_status_path_for(spreadsheet: str | Path) -> Path:
    """Where earlier versions kept it: next to the spreadsheet."""
    spreadsheet = Path(spreadsheet)
    return spreadsheet.with_name(spreadsheet.name + ".status.json")


class RunState:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.entries: dict[str, dict] = {}
        self.unsaved = False
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.entries = data.get("contacts", {})

    @classmethod
    def for_spreadsheet(cls, spreadsheet: str | Path) -> "RunState":
        path = status_path_for(spreadsheet)
        old = old_status_path_for(spreadsheet)
        if not path.exists() and old.exists():
            # Carry over progress from the old location, then retire the old file.
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(old, path)
            try:
                old.rename(old.with_name(old.name + ".moved"))
            except OSError:
                pass
        return cls(path)

    def status(self, key: str) -> str:
        return self.entries.get(key, {}).get("status", PENDING)

    def is_done(self, key: str) -> bool:
        return self.status(key) == DONE

    def mark(self, key: str, status: str, *, row_number: int | None = None,
             name: str | None = None, reason: str | None = None, **extra) -> None:
        if status not in STATUSES:
            raise ValueError(f"Unknown status {status!r}")
        entry = self.entries.setdefault(key, {})
        entry.update({
            "status": status,
            "row": row_number if row_number is not None else entry.get("row"),
            "name": name or entry.get("name"),
            "reason": reason,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        })
        entry["attempts"] = entry.get("attempts", 0) + (status != PENDING)
        entry.update(extra)
        self.save()

    def remember(self, key: str, **fields) -> None:
        """Store extra per-contact data (e.g. a typed-in email) without changing status."""
        self.entries.setdefault(key, {"status": PENDING}).update(fields)
        self.save()

    def recall(self, key: str, field: str, default=None):
        return self.entries.get(key, {}).get(field, default)

    def reset(self) -> None:
        self.entries = {}
        if self.path.exists():
            self.path.unlink()

    def counts(self, keys) -> dict[str, int]:
        result = {s: 0 for s in STATUSES}
        for key in keys:
            result[self.status(key)] += 1
        return result

    def save(self, attempts: int = 8) -> None:
        """Write the whole file. Never raises: if the file stays locked (antivirus,
        backup or sync software), progress is kept in memory and written with the
        next change."""
        payload = json.dumps({"version": SCHEMA_VERSION, "contacts": self.entries},
                             indent=2, ensure_ascii=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        wait = 0.25
        for attempt in range(attempts):
            try:
                tmp.write_text(payload, encoding="utf-8")
                os.replace(tmp, self.path)  # atomic, so a crash never leaves half a file
                if self.unsaved:
                    log.info("Progress file saved again after being locked.")
                self.unsaved = False
                return
            except OSError as exc:
                error = exc
                time.sleep(wait)
                wait = min(wait * 2, 2.0)
        try:
            self.path.write_text(payload, encoding="utf-8")  # not atomic, but saved
            self.unsaved = False
            return
        except OSError:
            pass
        self.unsaved = True
        log.warning("Couldn't save the progress file %s (%s); will retry with the next change.",
                    self.path, error)
