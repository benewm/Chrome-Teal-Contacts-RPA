"""Decide whether what Teal shows matches the spreadsheet.

Only fields with a value in the spreadsheet are checked (a blank cell means
"no opinion"). Formatting differences don't count: phone numbers compare by
digits, emails and dropdowns ignore case, dates compare as dates.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .spreadsheet import Contact, normalize_linkedin_url, parse_date

LABELS = {
    "name": "Name",
    "headline": "Title at Company",
    "email": "Email",
    "phone": "Phone Number",
    "url": "LinkedIn",
    "twitter": "Twitter",
    "relationship": "Relationship",
    "goal": "Goal",
    "status": "Status",
    "follow_up": "Follow up",
    "last_contacted": "Last contacted",
}
# Fields changed through the record's Edit form; the rest are set on the page.
FORM_FIELDS = ("name", "headline", "email", "phone", "url", "twitter")
CHOICE_FIELDS = ("relationship", "goal", "status")
DATE_FIELDS = ("follow_up", "last_contacted")


@dataclass
class Mismatch:
    field: str
    expected: str
    actual: str

    def __str__(self) -> str:
        return f"{LABELS[self.field]}: Teal shows {self.actual or '(blank)'!r}, sheet has {self.expected!r}"


def expected_values(contact: Contact, email: str, phone: str) -> dict[str, str]:
    headline = f"{contact.title} at {contact.company}" if contact.title and contact.company else ""
    return {
        "name": contact.name if contact.first_name or contact.last_name else "",
        "headline": headline,
        "email": email,
        "phone": phone,
        "url": contact.url,
        "twitter": contact.twitter,
        "relationship": contact.relationship,
        "goal": contact.goal,
        "status": contact.status,
        "follow_up": contact.follow_up,
        "last_contacted": contact.last_contacted,
    }


def _text(value: str) -> str:
    return " ".join(value.split()).casefold()


def _phone(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    return digits[1:] if len(digits) == 11 and digits.startswith("1") else digits


def _date(value: str):
    try:
        return parse_date(value)
    except ValueError:
        return value


def _twitter(value: str) -> str:
    value = value.strip().casefold().rstrip("/")
    value = re.sub(r"^https?://(www\.)?(twitter|x)\.com/", "", value)
    return value.lstrip("@")


NORMALIZERS = {
    "phone": _phone,
    "url": normalize_linkedin_url,
    "twitter": _twitter,
    "follow_up": _date,
    "last_contacted": _date,
}


def same(field: str, expected: str, actual: str) -> bool:
    normalize = NORMALIZERS.get(field, _text)
    return normalize(expected or "") == normalize(actual or "")


def compare(expected: dict[str, str], actual: dict[str, str]) -> list[Mismatch]:
    return [
        Mismatch(field, value, actual.get(field, ""))
        for field, value in expected.items()
        if value and not same(field, value, actual.get(field, ""))
    ]
