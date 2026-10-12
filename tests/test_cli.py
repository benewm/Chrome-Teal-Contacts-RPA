from teal_contacts import main

from .test_spreadsheet import make_csv

HEADERS = ["URL", "First Name", "Last Name", "Title", "Company", "Email", "Phone",
           "teal_contact_id", "rpa_status"]


def test_rows_marked_done_in_the_sheet_are_skipped(tmp_path, capsys):
    path = make_csv(tmp_path / "c.csv", HEADERS, [
        ["https://www.linkedin.com/in/a", "Ann", "A", "T", "C", "a@x.com", "1", "id-1", "done"],
        ["https://www.linkedin.com/in/b", "Bob", "B", "T", "C", "", "", "", "needs attention"],
    ])
    assert main([str(path), "--check"]) == 0
    out = capsys.readouterr().out
    assert "Bob B" in out and "Ann A" not in out
    assert "progress file:" in out and "app-data" in out


def test_nothing_to_do_when_sheet_says_all_done(tmp_path, capsys):
    path = make_csv(tmp_path / "c.csv", HEADERS, [
        ["https://www.linkedin.com/in/a", "Ann", "A", "T", "C", "a@x.com", "1", "id-1", "done"],
    ])
    assert main([str(path)]) == 0
    assert "Nothing to do." in capsys.readouterr().out
