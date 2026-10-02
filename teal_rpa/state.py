"""Per-spreadsheet run tracking, stored as <spreadsheet>.status.json next to it.

Entries are keyed by the normalized LinkedIn URL (Contact.key), so sorting or
inserting rows in the spreadsheet doesn't confuse which contacts are done.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

PENDING = "pending"
DONE = "done"
FAILED = "failed"
SKIPPED = "skipped"
STATUSES = (PENDING, DONE, FAILED, SKIPPED)

SCHEMA_VERSION = 1


def status_path_for(spreadsheet: str | Path) -> Path:
    spreadsheet = Path(spreadsheet)
    return spreadsheet.with_name(spreadsheet.name + ".status.json")


class RunState:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.entries: dict[str, dict] = {}
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.entries = data.get("contacts", {})

    @classmethod
    def for_spreadsheet(cls, spreadsheet: str | Path) -> "RunState":
        return cls(status_path_for(spreadsheet))

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

    def save(self) -> None:
        payload = {"version": SCHEMA_VERSION, "contacts": self.entries}
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)  # atomic, so a crash never leaves half a file
