from teal_rpa.teal import CHOICE_OPTIONS, match_kind, objects_with_ids, pick_option

ED_ID = "ed81f2eb-de5f-47fb-b31b-229bd2a90f48"


def test_pick_option_ignores_case_and_checkmarks():
    assert pick_option("Co-Worker", CHOICE_OPTIONS["relationship"]) == "Co-worker"
    assert pick_option("hiring manager", CHOICE_OPTIONS["relationship"]) == "Hiring manager"
    assert pick_option("Mentor", ["\u2713 Mentor", "Self"]) == "\u2713 Mentor"
    assert pick_option("Mentr", CHOICE_OPTIONS["relationship"]) is None


def test_finds_ids_at_any_depth():
    reply = {"data": {"insert_contacts_one": {
        "id": ED_ID, "first_name": "Ed", "last_name": "Soo Hoo",
        "linkedin_url": "https://www.linkedin.com/in/edsoohoo/",
        "company": {"id": "11111111-2222-3333-4444-555555555555", "name": "Lenovo"}}}}
    found = objects_with_ids(reply)
    assert {o["id"] for o in found} == {ED_ID, "11111111-2222-3333-4444-555555555555"}
    ed = next(o for o in found if o["id"] == ED_ID)
    assert match_kind(ed, "Ed", "Soo Hoo", "https://www.linkedin.com/in/edsoohoo") == "url"
    assert match_kind(ed, "Ed", "Soo Hoo", "https://www.linkedin.com/in/someone-else") == "name"
    company = next(o for o in found if o["id"] != ED_ID)
    assert match_kind(company, "Ed", "Soo Hoo", "https://www.linkedin.com/in/edsoohoo") is None


def test_ignores_non_uuid_ids():
    assert objects_with_ids([{"id": 7, "name": "Mentor"}, {"id": "7"}]) == []
