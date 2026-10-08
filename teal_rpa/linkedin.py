"""Look up a contact's email and phone on their LinkedIn "Contact info" overlay.

Opens <profile>/overlay/contact-info/ in its own tab and reads the dialog's
"Email" and "Phone" sections. LinkedIn only shows these when the person has
shared them with you (usually 1st-degree connections), so blanks are normal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import unquote

from playwright.sync_api import BrowserContext, Page
from playwright.sync_api import Error as PlaywrightError

LINKEDIN_ORIGIN = "https://www.linkedin.com"
DIALOG_TITLE = "Contact info"
# Where LinkedIn sends you when you're signed out.
LOGIN_URL_MARKERS = ("/login", "/authwall", "/checkpoint", "/uas/", "/signup")
NOT_FOUND_TEXT = re.compile(r"page (doesn.t|does not) exist|profile (is )?not available", re.I)

EMAIL_RE = re.compile(r"[\w.+'-]+@[\w-]+(\.[\w-]+)+")
PHONE_LABEL_RE = re.compile(r"\s*\((mobile|work|home|other)\)\s*$", re.I)


class LinkedInLoginRequired(Exception):
    pass


class LinkedInError(Exception):
    pass


@dataclass
class ContactInfo:
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    note: str | None = None  # why nothing was found, if known
    dialog_text: str = ""    # what the Contact info box showed, for troubleshooting

    @property
    def email(self) -> str:
        return self.emails[0] if self.emails else ""

    @property
    def phone(self) -> str:
        return self.phones[0] if self.phones else ""


def contact_info_url(profile_url: str, origin: str = LINKEDIN_ORIGIN) -> str:
    path = profile_url.split("linkedin.com", 1)[-1].rstrip("/")
    return f"{origin}{path}/overlay/contact-info/"


def parse_contact_info(text: str, mailto_hrefs: list[str] | str | None = None) -> ContactInfo:
    """Pull every email and phone number out of the dialog's visible text.

    The dialog reads like:  Phone / 203-554-7770 (Mobile) / Email / x@y.com
    """
    if isinstance(mailto_hrefs, str):
        mailto_hrefs = [mailto_hrefs]
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    emails: list[str] = []

    def add_email(value: str) -> None:
        value = value.strip()
        if value and value.lower() not in (e.lower() for e in emails):
            emails.append(value)

    for href in mailto_hrefs or []:
        if href and href.lower().startswith("mailto:"):
            add_email(unquote(href[7:].split("?")[0]))
    # The box only holds contact details, so anything shaped like an email
    # address in it is theirs (covers headings worded differently).
    for match in EMAIL_RE.finditer(text):
        add_email(match.group(0))

    phones: list[str] = []
    for i, line in enumerate(lines):
        if line.lower() != "phone":
            continue
        # Every phone-looking line under the heading, until the next section.
        for following in lines[i + 1:]:
            candidate = PHONE_LABEL_RE.sub("", following).strip()
            digits = re.sub(r"\D", "", candidate)
            if len(digits) < 7 or len(candidate) > 40 or EMAIL_RE.search(candidate):
                break
            if digits not in (re.sub(r"\D", "", p) for p in phones):
                phones.append(candidate)
        break

    return ContactInfo(emails=emails, phones=phones)


class LinkedInPage:
    def __init__(self, context: BrowserContext, timeout_ms: int = 20_000,
                 origin: str = LINKEDIN_ORIGIN):
        self.context = context
        self.timeout_ms = timeout_ms
        self.origin = origin
        self._page: Page | None = None

    @property
    def page(self) -> Page:
        if self._page is None or self._page.is_closed():
            self._page = self.context.new_page()
        return self._page

    def lookup(self, profile_url: str) -> ContactInfo:
        page = self.page
        url = contact_info_url(profile_url, self.origin)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        except PlaywrightError as exc:
            raise LinkedInError(f"LinkedIn didn't load: {_short(exc)}") from exc
        self._check_signed_in(page)

        dialog = page.get_by_role("dialog").filter(has_text=DIALOG_TITLE).first
        try:
            dialog.wait_for(state="visible", timeout=self.timeout_ms)
        except PlaywrightError as exc:
            self._check_signed_in(page)
            if NOT_FOUND_TEXT.search(_body_text(page)):
                return ContactInfo(note="LinkedIn says this profile doesn't exist")
            raise LinkedInError("LinkedIn's Contact info box didn't appear") from exc

        # Sections fill in after the dialog opens; the profile link is always
        # there, so once it shows the rest has loaded too.
        try:
            dialog.get_by_text(re.compile(r"linkedin\.com/in/", re.I)).first.wait_for(
                state="visible", timeout=5_000)
        except PlaywrightError:
            pass

        try:
            text = dialog.inner_text(timeout=self.timeout_ms)
            hrefs = dialog.locator('a[href^="mailto:"]').evaluate_all(
                "links => links.map(a => a.getAttribute('href'))")
        except PlaywrightError as exc:
            raise LinkedInError(f"Couldn't read LinkedIn's Contact info: {_short(exc)}") from exc

        info = parse_contact_info(text, hrefs)
        info.dialog_text = text
        if not (info.email or info.phone):
            info.note = "no email or phone shared on LinkedIn"
        return info

    def _check_signed_in(self, page: Page) -> None:
        if any(marker in page.url for marker in LOGIN_URL_MARKERS):
            raise LinkedInLoginRequired("LinkedIn wants you to sign in")


def _body_text(page: Page) -> str:
    try:
        return page.locator("body").inner_text(timeout=2_000)
    except PlaywrightError:
        return ""


def _short(exc: Exception) -> str:
    return str(exc).strip().splitlines()[0][:200]
