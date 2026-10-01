import csv
import datetime

import openpyxl
import pytest

from teal_rpa.spreadsheet import (
    SpreadsheetError,
    SpreadsheetLockedError,
    load_contacts,
    normalize_linkedin_url,
    write_back,
)

# Same layout as Teal's contact export, including its repeated columns.
TEAL_HEADERS = [
    "First Name", "Last Name", "Email Address", "Phone", "Excitement", "Position",
    "Company", "Location", "URL", "source", "follow_up_at",
    "contact_intention_type", "contact_intention_type",
]


def make_xlsx(path, headers, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Upload 1"
    ws.append(headers)
    for row in rows:
        ws.append(row)
    wb.save(path)
    return path


def make_csv(path, headers, rows, encoding="utf-8-sig"):
    with path.open("w", newline="", encoding=encoding) as f:
        w = csv.writer(f)
        w.writerow(headers)
        w.writerows(rows)
    return path


def teal_row(first, last, url, email=None, phone=None, position="CTO", company="Acme"):
    return [first, last, email, phone, None, position, company, None, url,
            "ChromeExtension", datetime.datetime(2026, 10, 2), '{"name":"Networking","id":"1"}', "x"]


def test_loads_teal_export_xlsx(tmp_path):
    path = make_xlsx(tmp_path / "c.xlsx", TEAL_HEADERS, [
        teal_row("Ed", "Soo Hoo", "https://www.linkedin.com/in/edsoohoo", phone=5551234567),
    ])
    sheet = load_contacts(path)
    assert sheet.sheet_name == "Upload 1"
    [c] = sheet.contacts
    assert (c.first_name, c.last_name, c.title, c.company) == ("Ed", "Soo Hoo", "CTO", "Acme")
    assert c.url == "https://www.linkedin.com/in/edsoohoo"
    assert c.email == ""
    assert c.phone == "5551234567"  # not "5551234567.0"
    assert c.row_number == 2
    assert c.raw["follow_up_at"] == "2026-10-02"
    assert c.is_valid
    assert any("Duplicate column 'contact_intention_type'" in w for w in sheet.warnings)


def test_loads_plain_headers_csv(tmp_path):
    path = make_csv(tmp_path / "c.csv",
                    ["LinkedIn URL", "First Name", "Last Name", "Title", "Company", "Email", "Phone"],
                    [["linkedin.com/in/jane/", "Jane", "Doe", "VP", "Globex", "j@x.com", ""]])
    [c] = load_contacts(path).contacts
    assert c.url == "https://www.linkedin.com/in/jane"
    assert (c.title, c.email, c.phone) == ("VP", "j@x.com", "")


def test_cp1252_csv(tmp_path):
    path = make_csv(tmp_path / "c.csv", ["URL", "First Name", "Last Name", "Title", "Company"],
                    [["https://www.linkedin.com/in/jose", "José", "Núñez", "CTO", "Acme"]],
                    encoding="cp1252")
    assert load_contacts(path).contacts[0].first_name == "José"


def test_missing_required_columns(tmp_path):
    path = make_csv(tmp_path / "c.csv", ["First Name", "Last Name", "URL"], [["a", "b", "c"]])
    with pytest.raises(SpreadsheetError, match="title.*company|company"):
        load_contacts(path)


def test_flags_bad_rows_and_skips_blank_lines(tmp_path):
    path = make_xlsx(tmp_path / "c.xlsx", TEAL_HEADERS, [
        teal_row("No", "Url", None),
        [None] * len(TEAL_HEADERS),
        teal_row("Not", "Linkedin", "https://example.com/someone"),
        teal_row("First", "Copy", "https://www.linkedin.com/in/dup"),
        teal_row("Second", "Copy", "https://linkedin.com/in/dup/?trk=x"),
    ])
    contacts = load_contacts(path).contacts
    assert [c.row_number for c in contacts] == [2, 4, 5, 6]
    problems = {c.name: c.problems for c in contacts}
    assert problems["No Url"] == ["missing LinkedIn URL"]
    assert "not a LinkedIn profile URL" in problems["Not Linkedin"][0]
    assert problems["First Copy"] == []
    assert problems["Second Copy"] == ["duplicate of row 5"]
    assert contacts[0].key == "row:2"


def test_unsupported_and_missing_file(tmp_path):
    with pytest.raises(SpreadsheetError, match="not found"):
        load_contacts(tmp_path / "nope.xlsx")
    (tmp_path / "c.txt").write_text("x")
    with pytest.raises(SpreadsheetError, match="Unsupported"):
        load_contacts(tmp_path / "c.txt")


@pytest.mark.parametrize("raw,expected", [
    ("https://www.linkedin.com/in/edsoohoo", "https://www.linkedin.com/in/edsoohoo"),
    ("http://linkedin.com/in/edsoohoo/", "https://www.linkedin.com/in/edsoohoo"),
    ("www.linkedin.com/in/edsoohoo?utm=1#top", "https://www.linkedin.com/in/edsoohoo"),
    ("  ", ""),
])
def test_normalize_linkedin_url(raw, expected):
    assert normalize_linkedin_url(raw) == expected


def test_write_back_xlsx_keeps_other_cells(tmp_path):
    path = make_xlsx(tmp_path / "c.xlsx", TEAL_HEADERS, [
        teal_row("Ed", "Soo Hoo", "https://www.linkedin.com/in/edsoohoo"),
        teal_row("Josh", "R", "https://www.linkedin.com/in/josh"),
    ])
    sheet = load_contacts(path)
    write_back(sheet, 3, {"email": "josh@x.com", "phone": "555"})
    reloaded = load_contacts(path).contacts
    assert (reloaded[1].email, reloaded[1].phone) == ("josh@x.com", "555")
    assert reloaded[0].email == ""
    assert reloaded[1].raw["source"] == "ChromeExtension"


def test_write_back_csv(tmp_path):
    path = make_csv(tmp_path / "c.csv", ["URL", "First Name", "Last Name", "Title", "Company", "Email"],
                    [["https://www.linkedin.com/in/a", "A", "B", "T", "C", ""]])
    sheet = load_contacts(path)
    write_back(sheet, 2, {"email": "a@b.com", "phone": "ignored, no column"})
    assert load_contacts(path).contacts[0].email == "a@b.com"


def test_write_back_locked_file(tmp_path, monkeypatch):
    path = make_csv(tmp_path / "c.csv", ["URL", "First Name", "Last Name", "Title", "Company", "Email"],
                    [["https://www.linkedin.com/in/a", "A", "B", "T", "C", ""]])
    sheet = load_contacts(path)

    def locked(*args, **kwargs):
        raise PermissionError("in use")

    monkeypatch.setattr("teal_rpa.spreadsheet._write_back_csv", locked)
    with pytest.raises(SpreadsheetLockedError, match="open in Excel"):
        write_back(sheet, 2, {"email": "a@b.com"})
