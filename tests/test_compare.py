from teal_rpa.compare import compare, expected_values, same
from teal_rpa.spreadsheet import Contact


def contact(**kw):
    base = dict(row_number=2, url="https://www.linkedin.com/in/edsoohoo", first_name="Ed",
                last_name="Soo Hoo", title="WW CTO Global Accounts", company="Lenovo",
                email="", phone="")
    base.update(kw)
    return Contact(**base)


# What Teal showed for Ed in the screenshot of a finished record.
TEAL_ED = {
    "name": "Ed Soo Hoo", "headline": "WW CTO Global Accounts at Lenovo",
    "email": "esoohoo@lenovo.com", "url": "https://www.linkedin.com/in/edsoohoo",
    "phone": "", "twitter": "", "relationship": "Mentor", "goal": "Networking",
    "status": "Follow up needed", "follow_up": "2026-10-16", "last_contacted": "2026-10-05",
}


def test_finished_record_matches():
    c = contact(relationship="Mentor", goal="Networking", status="Follow up needed",
                follow_up="2026-10-16")
    assert compare(expected_values(c, "ESooHoo@Lenovo.com", ""), TEAL_ED) == []


def test_reports_differences_with_labels():
    c = contact(relationship="Co-worker", follow_up="2026-10-17")
    diffs = compare(expected_values(c, "esoohoo@lenovo.com", "203-555-0100"), TEAL_ED)
    assert [d.field for d in diffs] == ["phone", "relationship", "follow_up"]
    assert str(diffs[0]) == "Phone Number: Teal shows '(blank)', sheet has '203-555-0100'"


def test_blank_sheet_values_are_not_checked():
    c = contact(title="", company="")
    assert compare(expected_values(c, "", ""),
                   {"name": "Ed Soo Hoo", "url": "https://www.linkedin.com/in/edsoohoo"}) == []


def test_formatting_differences_are_ignored():
    assert same("phone", "203-555-0100", "(203) 555-0100")
    assert same("phone", "+1 203 555 0100", "203.555.0100")
    assert same("url", "https://www.linkedin.com/in/edsoohoo", "linkedin.com/in/edsoohoo/")
    assert same("relationship", "Co-Worker", "Co-worker")
    assert same("follow_up", "2026-10-16", "10/16/2026")
    assert same("twitter", "@ed", "https://x.com/ed")
    assert same("headline", "CTO  at Acme", "CTO at Acme")
    assert not same("phone", "203-555-0100", "203-555-0101")
