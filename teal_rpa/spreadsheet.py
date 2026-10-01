"""Read the contacts spreadsheet (.xlsx or .csv) and write values back to it."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit

# Canonical field -> accepted header names (matched case-insensitively).
# Covers both the plain names ("LinkedIn URL", "Title") and Teal's export
# names ("URL", "Position", "Email Address").
COLUMN_ALIASES: dict[str, list[str]] = {
    "url": ["LinkedIn URL", "URL", "Link", "LinkedIn", "Profile URL"],
    "first_name": ["First Name", "FirstName"],
    "last_name": ["Last Name", "LastName"],
    "title": ["Title", "Position", "Job Title"],
    "company": ["Company", "Company Name"],
    "email": ["Email", "Email Address", "E-mail"],
    "phone": ["Phone", "Phone Number", "Mobile"],
}
REQUIRED_FIELDS = ["url", "first_name", "last_name", "title", "company"]
OPTIONAL_FIELDS = ["email", "phone"]

LINKEDIN_PROFILE_RE = re.compile(r"^https://www\.linkedin\.com/in/[^/]+$")


class SpreadsheetError(Exception):
    """The spreadsheet can't be used as-is (bad format, missing columns...)."""


class SpreadsheetLockedError(SpreadsheetError):
    """The file couldn't be written, usually because it's open in Excel."""


@dataclass
class Contact:
    row_number: int  # 1-based row as shown in Excel (header is row 1)
    url: str
    first_name: str
    last_name: str
    title: str
    company: str
    email: str
    phone: str
    raw: dict[str, str] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip() or f"row {self.row_number}"

    @property
    def key(self) -> str:
        """Stable id for run tracking: the normalized URL, else the row number."""
        return self.url or f"row:{self.row_number}"

    @property
    def is_valid(self) -> bool:
        return not self.problems


@dataclass
class Sheet:
    path: Path
    sheet_name: str | None
    headers: list[str]
    column_map: dict[str, int]  # canonical field -> 0-based column index
    contacts: list[Contact]
    warnings: list[str]


def normalize_linkedin_url(value: str) -> str:
    """Canonical form: https://www.linkedin.com/in/<slug> (no query, no slash)."""
    value = value.strip()
    if not value:
        return ""
    if not re.match(r"^https?://", value, re.I):
        value = "https://" + value
    parts = urlsplit(value)
    host = parts.netloc.lower()
    if host.endswith("linkedin.com"):
        host = "www.linkedin.com"
    path = parts.path.rstrip("/")
    if path.lower().startswith("/in/"):
        path = "/in/" + path[4:]
    return f"https://{host}{path}"


def _cell_to_str(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        # Phone numbers typed into Excel come back as 5551234567.0
        return str(int(value))
    if isinstance(value, datetime):
        return value.isoformat(sep=" ") if value.time() != datetime.min.time() else value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip()


def _map_columns(headers: list[str]) -> tuple[dict[str, int], list[str]]:
    lookup: dict[str, int] = {}
    warnings: list[str] = []
    for index, header in enumerate(headers):
        norm = header.strip().lower()
        if not norm:
            continue
        if norm in lookup:
            warnings.append(f"Duplicate column '{header}' (column {index + 1}); using the first one.")
            continue
        lookup[norm] = index

    column_map: dict[str, int] = {}
    for canonical, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias.lower() in lookup:
                column_map[canonical] = lookup[alias.lower()]
                break

    missing = [f for f in REQUIRED_FIELDS if f not in column_map]
    if missing:
        wanted = "; ".join(f"{f}: one of {COLUMN_ALIASES[f]}" for f in missing)
        raise SpreadsheetError(
            f"Missing required column(s) -> {wanted}. Found headers: {[h for h in headers if h]}"
        )
    return column_map, warnings


def _read_rows(path: Path, sheet_name: str | None) -> tuple[list[list], str | None]:
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        import openpyxl

        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            if sheet_name and sheet_name not in wb.sheetnames:
                raise SpreadsheetError(f"Sheet '{sheet_name}' not found. Sheets: {wb.sheetnames}")
            ws = wb[sheet_name] if sheet_name else wb.worksheets[0]
            return [list(r) for r in ws.iter_rows(values_only=True)], ws.title
        finally:
            wb.close()
    if suffix == ".csv":
        return _read_csv(path)[0], None
    raise SpreadsheetError(f"Unsupported file type '{path.suffix}'. Use .xlsx or .csv.")


def _read_csv(path: Path) -> tuple[list[list[str]], str]:
    # Excel on Windows saves CSV as UTF-8 with BOM or as cp1252.
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            with path.open(newline="", encoding=encoding) as f:
                return list(csv.reader(f)), encoding
        except UnicodeDecodeError:
            continue
    raise SpreadsheetError(f"Can't decode {path} as UTF-8 or Windows-1252.")


def load_contacts(path: str | Path, sheet_name: str | None = None) -> Sheet:
    path = Path(path)
    if not path.exists():
        raise SpreadsheetError(f"File not found: {path}")

    rows, actual_sheet = _read_rows(path, sheet_name)
    if not rows:
        raise SpreadsheetError(f"{path} is empty.")

    headers = [_cell_to_str(h) for h in rows[0]]
    column_map, warnings = _map_columns(headers)

    contacts: list[Contact] = []
    seen_urls: dict[str, int] = {}
    for offset, row in enumerate(rows[1:]):
        row_number = offset + 2
        cells = [_cell_to_str(v) for v in row]
        if not any(cells):
            continue  # blank line
        cells += [""] * (len(headers) - len(cells))

        def get(name: str) -> str:
            idx = column_map.get(name)
            return cells[idx] if idx is not None else ""

        raw = {}
        for h, v in zip(headers, cells):
            if h and h not in raw:
                raw[h] = v

        url = normalize_linkedin_url(get("url"))
        contact = Contact(
            row_number=row_number,
            url=url,
            first_name=get("first_name"),
            last_name=get("last_name"),
            title=get("title"),
            company=get("company"),
            email=get("email"),
            phone=get("phone"),
            raw=raw,
        )
        if not url:
            contact.problems.append("missing LinkedIn URL")
        elif not LINKEDIN_PROFILE_RE.match(url):
            contact.problems.append(f"not a LinkedIn profile URL: {get('url')}")
        elif url in seen_urls:
            contact.problems.append(f"duplicate of row {seen_urls[url]}")
        else:
            seen_urls[url] = row_number
        if not (contact.first_name or contact.last_name):
            contact.problems.append("missing first and last name")
        contacts.append(contact)

    return Sheet(path, actual_sheet, headers, column_map, contacts, warnings)


def write_back(sheet: Sheet, row_number: int, values: dict[str, str]) -> None:
    """Write canonical fields (e.g. {"email": ..., "phone": ...}) into a row.

    Columns that don't exist in the sheet are skipped. Raises
    SpreadsheetLockedError if the file is open elsewhere (Excel locks it).
    """
    updates = {sheet.column_map[k]: v for k, v in values.items() if k in sheet.column_map}
    if not updates:
        return
    try:
        if sheet.path.suffix.lower() == ".csv":
            _write_back_csv(sheet.path, row_number, updates)
        else:
            _write_back_xlsx(sheet.path, sheet.sheet_name, row_number, updates)
    except PermissionError as exc:
        raise SpreadsheetLockedError(
            f"Can't write to {sheet.path.name} - is it open in Excel? Close it and re-run."
        ) from exc


def _write_back_xlsx(path: Path, sheet_name: str | None, row_number: int, updates: dict[int, str]) -> None:
    import openpyxl

    wb = openpyxl.load_workbook(path)
    ws = wb[sheet_name] if sheet_name else wb.worksheets[0]
    for col_index, value in updates.items():
        ws.cell(row=row_number, column=col_index + 1, value=value)
    wb.save(path)


def _write_back_csv(path: Path, row_number: int, updates: dict[int, str]) -> None:
    rows, encoding = _read_csv(path)
    row = rows[row_number - 1]
    for col_index, value in updates.items():
        row += [""] * (col_index + 1 - len(row))
        row[col_index] = value
    with path.open("w", newline="", encoding=encoding) as f:
        csv.writer(f).writerows(rows)
