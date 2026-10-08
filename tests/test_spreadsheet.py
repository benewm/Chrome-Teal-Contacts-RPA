import csv
import datetime

import openpyxl
import pytest

from teal_rpa.spreadsheet import (
    SpreadsheetError,
    SpreadsheetLockedError,
    choice_name,
    ensure_columns,
    load_contacts,
    normalize_linkedin_url,
    parse_date,
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
    assert any("'contact_intention_type' appears 2 times" in w for w in sheet.warnings)


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
    monkeypatch.setattr("teal_rpa.spreadsheet.time.sleep", lambda s: None)
    with pytest.raises(SpreadsheetLockedError, match="open in Excel"):
        write_back(sheet, 2, {"email": "a@b.com"})


NETWORKING_HEADERS = ["First Name", "Last Name", "Position", "Company", "URL", "follow_up_at",
                      "contact_relationship_type", "contact_intention_type", "contact_next_step_type",
                      "contact_relationship_type", "contact_intention_type", "contact_next_step_type"]


def test_reads_networking_columns(tmp_path):
    mentor, networking, follow = ('{"name":"Mentor","id":"7"}', '{"name":"Networking","id":"1"}',
                                  '{"name":"Follow up needed","id":"2"}')
    path = make_xlsx(tmp_path / "c.xlsx", NETWORKING_HEADERS, [
        ["Ed", "Soo Hoo", "CTO", "Lenovo", "https://www.linkedin.com/in/ed",
         datetime.datetime(2026, 10, 16), mentor, networking, follow, mentor, networking, follow],
    ])
    [c] = load_contacts(path).contacts
    assert (c.relationship, c.goal, c.status, c.follow_up) == \
        ("Mentor", "Networking", "Follow up needed", "2026-10-16")
    assert c.warnings == []


def test_repeated_columns_first_non_empty_and_conflicts(tmp_path):
    path = make_xlsx(tmp_path / "c.xlsx", NETWORKING_HEADERS, [
        ["A", "B", "T", "C", "https://www.linkedin.com/in/a", "10/16/2026",
         None, "Networking", '{"name":"To be contacted"}', "Friend", "networking", "Thank you sent"],
    ])
    [c] = load_contacts(path).contacts
    assert c.relationship == "Friend"  # first copy blank, second used
    assert c.goal == "Networking"      # same value, different case: no warning
    assert c.status == "To be contacted"
    assert c.follow_up == "2026-10-16"
    assert c.warnings == ["contact_next_step_type has different values in its repeated "
                          "columns; using 'To be contacted'"]


def test_bad_date_is_a_warning_not_a_problem(tmp_path):
    path = make_xlsx(tmp_path / "c.xlsx", NETWORKING_HEADERS[:6], [
        ["A", "B", "T", "C", "https://www.linkedin.com/in/a", "next week"]])
    [c] = load_contacts(path).contacts
    assert c.is_valid and c.follow_up == ""
    assert "isn't a date" in c.warnings[0]


@pytest.mark.parametrize("raw,expected", [
    ('{"name":"Co-Worker","id":"2"}', "Co-Worker"), ("Mentor", "Mentor"), ("", ""),
    ("{not json", "{not json"),
])
def test_choice_name(raw, expected):
    assert choice_name(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("2026-10-16", datetime.date(2026, 10, 16)), ("2026-10-16 09:30:00", datetime.date(2026, 10, 16)),
    ("2026-10-16T09:30:00Z", datetime.date(2026, 10, 16)), ("10/2/2026", datetime.date(2026, 10, 2)),
    ("", None),
])
def test_parse_date(raw, expected):
    assert parse_date(raw) == expected


@pytest.mark.parametrize("make", ["xlsx", "csv"])
def test_ensure_columns_adds_tool_columns_once(tmp_path, make):
    headers = ["URL", "First Name", "Last Name", "Title", "Company", "Email"]
    data = [["https://www.linkedin.com/in/a", "A", "B", "T", "C", ""]]
    path = (make_xlsx(tmp_path / "c.xlsx", headers, data) if make == "xlsx"
            else make_csv(tmp_path / "c.csv", headers, data))
    sheet = load_contacts(path)
    assert ensure_columns(sheet, ["teal_id", "rpa_status"]) == ["teal_contact_id", "rpa_status"]
    write_back(sheet, 2, {"teal_id": "abc", "rpa_status": "done"})
    again = load_contacts(path)
    assert ensure_columns(again, ["teal_id", "rpa_status"]) == []
    assert again.contacts[0].teal_id == "abc"
    assert again.headers[-2:] == ["teal_contact_id", "rpa_status"]
