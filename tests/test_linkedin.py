from teal_rpa.linkedin import contact_info_url, parse_contact_info

SCREENSHOT_TEXT = """Contact info
Your profile
linkedin.com/in/benewm
Phone
203-554-7770 (Mobile)
Address
Trumbull, CT
Email
benewm@gmail.com
Edit contact info"""


def test_parses_the_contact_info_dialog():
    info = parse_contact_info(SCREENSHOT_TEXT, "mailto:benewm@gmail.com")
    assert (info.email, info.phone) == ("benewm@gmail.com", "203-554-7770")


def test_email_from_text_when_no_mailto_link():
    assert parse_contact_info(SCREENSHOT_TEXT).email == "benewm@gmail.com"


def test_nothing_shared():
    info = parse_contact_info("Contact info\nEd's Profile\nlinkedin.com/in/edsoohoo\nConnected\nMar 3, 2021")
    assert (info.email, info.phone) == ("", "")


def test_ignores_non_phone_text_and_other_labels():
    info = parse_contact_info("Phone\n+44 20 7946 0958 (Work)\nWebsites\nexample.com (Company)")
    assert info.phone == "+44 20 7946 0958"
    assert parse_contact_info("Phone\nAsk me\nEmail\nnot-shared").phone == ""
    assert parse_contact_info("Phone\nAsk me\nEmail\nnot-shared").email == ""


def test_mailto_with_encoding_and_params():
    assert parse_contact_info("", "mailto:jane.doe%2Bjobs@x.co.uk?subject=hi").email == "jane.doe+jobs@x.co.uk"


def test_contact_info_url():
    assert contact_info_url("https://www.linkedin.com/in/benewm") == \
        "https://www.linkedin.com/in/benewm/overlay/contact-info/"
    assert contact_info_url("https://www.linkedin.com/in/benewm", "http://127.0.0.1:9") == \
        "http://127.0.0.1:9/in/benewm/overlay/contact-info/"


def test_email_anywhere_in_box_when_heading_differs():
    text = "Contact info\nJosh's Profile\nlinkedin.com/in/joshreicher\nEmail address\njosh@example.com"
    assert parse_contact_info(text).email == "josh@example.com"
