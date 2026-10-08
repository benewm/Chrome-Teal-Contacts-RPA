"""Drive Teal's contact tracker on app.tealhq.com.

Two screens are involved:
  * the tracker list (/contact-tracker), with "+ Add a New Contact", whose
    form opens in an iframe; Save returns to the list;
  * a contact's page (/contact-tracker/<id>): Relationship, Goal and Status
    dropdowns, Follow up / Last contacted calendars (saved automatically, no
    Save button), Contact Information, and an Edit button for the basics.

If Teal changes its page, the strings below are the first thing to update.
Form boxes are found by their placeholder (the grey hint in an empty box);
page fields by the label shown above them.
"""

from __future__ import annotations

import re
import time
from datetime import date
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Frame, Locator, Page, Response

from .spreadsheet import normalize_linkedin_url, parse_date

TRACKER_URL = "https://app.tealhq.com/contact-tracker"
ADD_BUTTON = "Add a New Contact"  # button on the tracker page
SAVE_BUTTON = re.compile(r"^\s*(Save|Update)( Contact| Changes)?\s*$", re.I)  # in the forms
CANCEL_BUTTON = "Cancel"
EDIT_BUTTON = "Edit"              # on a contact's page

# Contact field -> placeholder of the matching box in Teal's Add/Edit form.
FIELD_PLACEHOLDERS: dict[str, str] = {
    "first_name": "First Name",
    "last_name": "Last Name",
    "title": "Job Title",
    "company": "Company Name",
    "email": "Email Address",
    "url": "LinkedIn Profile",
    "twitter": "Twitter Handle URL",
    "location": "Location",
    "phone": "Phone",
}
# Used to recognise the form among the page's frames.
FORM_MARKER_FIELD = "first_name"

# Fields on a contact's page: field -> (section heading, field label).
CHOICE_FIELDS = {
    "relationship": ("Networking", "Relationship"),
    "goal": ("Networking", "Goal"),
    "status": ("Networking", "Status"),
}
DATE_FIELDS = {
    "follow_up": ("Dates", "Follow up"),
    "last_contacted": ("Dates", "Last contacted"),
}
CONTACT_INFO_SECTION = "Contact Information"
CONTACT_INFO_LABELS = {"email": "Email", "url": "LinkedIn", "phone": "Phone Number", "twitter": "Twitter"}

# Teal's dropdown options (October 2026), used to check the spreadsheet up front.
# The tool always picks from what Teal actually offers, ignoring case.
CHOICE_OPTIONS = {
    "relationship": ["Self", "Co-worker", "Friend", "Family", "Other", "Recruiter",
                     "Mentor", "Hiring manager", "Alumni"],
    "goal": ["Networking", "Informational interview", "Request referral",
             "Research interviewer", "Research career"],
    "status": ["To be contacted", "Follow up needed", "Meeting scheduled", "Thank you sent"],
}

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]
MONTH_YEAR = re.compile(rf"\b({'|'.join(MONTHS)})\s+(\d{{4}})\b", re.I)
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
ID_KEYS = ("id", "uuid", "contact_id", "contactId", "_id")


# Is this calendar day a faded one from the month before/after? Checks whole
# class names (day-outside, rdp-outside, outside): shadcn puts styling classes
# such as "[&:has([aria-selected].day-outside)]:bg-accent/50" on every day, so
# a mere substring match would rule out every day.
OUTSIDE_DAY_JS = """e => [e, e.parentElement].some(n => n && (
    [...n.classList].some(c => /^(rdp-)?(day[-_])?outside$/i.test(c))
    || n.dataset?.outside === 'true'
    || n.getAttribute('aria-disabled') === 'true' || n.disabled === true))"""


class TealError(Exception):
    """Something went wrong that's safe to retry (nothing was created)."""


class NotLoggedIn(TealError):
    """Teal showed something other than the contact tracker (e.g. a sign-in page)."""


class SaveUnconfirmed(Exception):
    """Save was clicked but the form didn't close. Not safe to retry blindly:
    the contact may have been created."""


class NotAnOption(Exception):
    """The spreadsheet's value isn't one of the dropdown's options."""


def pick_option(value: str, options: list[str]) -> str | None:
    """The option matching value, ignoring case and stray spaces/checkmarks."""
    def norm(text: str) -> str:
        return " ".join(text.replace("✓", "").split()).casefold()
    wanted = norm(value)
    return next((o for o in options if norm(o) == wanted), None)


def objects_with_ids(data) -> list[dict]:
    """Every JSON object (at any depth) that has a UUID id, as
    {"id": ..., "strings": {lower-cased string values}}."""
    found = []
    stack = [data]
    while stack:
        item = stack.pop()
        if isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, dict):
            ident = next((item[k] for k in ID_KEYS
                          if isinstance(item.get(k), str) and UUID_RE.match(item[k])), None)
            if ident:
                strings = {v.strip().casefold() for v in item.values() if isinstance(v, str) and v.strip()}
                found.append({"id": ident, "strings": strings})
            stack.extend(v for v in item.values() if isinstance(v, (dict, list)))
    return found


def match_kind(obj: dict, first: str, last: str, url: str) -> str | None:
    """'url' if the object holds this LinkedIn URL, 'name' if just the name."""
    if url:
        for value in obj["strings"]:
            if "linkedin.com/in/" in value and normalize_linkedin_url(value) == url.casefold():
                return "url"
    if first and last and first.casefold() in obj["strings"] and last.casefold() in obj["strings"]:
        return "name"
    return None


class TealPage:
    def __init__(self, page: Page, tracker_url: str = TRACKER_URL, timeout_ms: int = 20_000):
        self.page = page
        self.tracker_url = tracker_url.rstrip("/")
        self.timeout_ms = timeout_ms
        self.known: list[dict] = []          # objects with ids seen in Teal's responses
        self.debug_dir: Path | None = None   # where to keep HTML of misbehaving widgets
        self._responses: list[Response] = []
        page.on("response", self._on_response)

    # -- Teal's own data, as the page receives it ---------------------------

    def _on_response(self, response: Response) -> None:
        # Only note it here; bodies are read later (no blocking calls in events).
        try:
            if response.request.resource_type in ("xhr", "fetch") and \
                    "json" in (response.headers.get("content-type") or ""):
                self._responses.append(response)
                del self._responses[:-200]
        except Exception:
            pass

    def harvest(self) -> list[dict]:
        """Read the JSON responses received since the last call."""
        responses, self._responses = self._responses, []
        found = []
        for response in responses:
            try:
                found.extend(objects_with_ids(response.json()))
            except Exception:
                continue
        self.known.extend(found)
        return found

    def current_id(self) -> str | None:
        tail = self.page.url.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
        return tail if UUID_RE.match(tail) else None

    # -- tracker list ---------------------------------------------------------

    def open_tracker(self, reload: bool = False) -> None:
        """Make sure the contact tracker is showing with its Add button."""
        try:
            if reload or self.page.url.split("?")[0].rstrip("/") != self.tracker_url:
                self.page.goto(self.tracker_url, wait_until="domcontentloaded",
                               timeout=self.timeout_ms)
            self._add_button().wait_for(state="visible", timeout=self.timeout_ms)
        except PlaywrightError as exc:
            self._raise_if_signed_out(exc)
            raise TealError(f"The '{ADD_BUTTON}' button didn't appear: {_short(exc)}") from exc
        self.page.wait_for_timeout(300)  # let the list's data arrive
        self.harvest()

    def _raise_if_signed_out(self, exc: Exception | None = None) -> None:
        url = self.page.url
        if not url.startswith(self.tracker_url):
            raise NotLoggedIn(f"Teal opened {url} instead of the contact tracker") from exc

    def _add_button(self) -> Locator:
        return self.page.get_by_role("button", name=ADD_BUTTON).first

    def ids_in_list(self, name: str) -> list[str]:
        """Open each entry in the tracker list called `name`; return their ids."""
        ids: list[str] = []
        self.open_tracker(reload=True)
        count = len(self._list_entries(name))
        for index in range(count):
            if index:
                self.open_tracker(reload=True)
            entries = self._list_entries(name)
            if index >= len(entries):
                break
            try:
                entries[index].click(timeout=self.timeout_ms)
                self.page.wait_for_url(re.compile(r"/contact-tracker/[0-9a-f-]{36}"),
                                       timeout=self.timeout_ms)
            except PlaywrightError:
                continue
            ident = self.current_id()
            if ident and ident not in ids:
                ids.append(ident)
        return ids

    def _list_entries(self, name: str) -> list[Locator]:
        # The name also appears as the open contact's heading; skip headings.
        candidates = self.page.get_by_text(name, exact=True)
        entries = []
        for i in range(candidates.count()):
            entry = candidates.nth(i)
            try:
                if entry.is_visible() and entry.evaluate(
                        "e => !e.closest('h1,h2,h3,[role=heading]')"):
                    entries.append(entry)
            except PlaywrightError:
                continue
        return entries

    def find_existing(self, first: str, last: str, url: str) -> str | None:
        """Id of a contact already in Teal with this LinkedIn URL, or None."""
        self.open_tracker()
        for obj in reversed(self.known):
            if match_kind(obj, first, last, url) == "url":
                return obj["id"]
        # Not in the data Teal sent; look the name up in the list and check
        # each match's LinkedIn on its page.
        name = f"{first} {last}".strip()
        unconfirmed = []
        for ident in self.ids_in_list(name):
            self.open_record(ident)
            record_url = self.read_record().get("url", "")
            if record_url and normalize_linkedin_url(record_url) == url:
                return ident
            if not record_url:
                unconfirmed.append(ident)
        # Same name and no LinkedIn saved: treat as the same person only if unique.
        return unconfirmed[0] if len(unconfirmed) == 1 else None

    # -- Add / Edit form ------------------------------------------------------

    def open_form(self) -> Frame:
        """Click "Add a New Contact" and return the frame holding the form."""
        self._close_stale_form()
        try:
            self._add_button().click(timeout=self.timeout_ms)
        except PlaywrightError as exc:
            raise TealError(f"Couldn't click '{ADD_BUTTON}': {_short(exc)}") from exc
        frame = self._find_form_frame(timeout_ms=self.timeout_ms)
        if frame is None:
            raise TealError("The Add a New Contact form didn't appear")
        return frame

    def open_edit_form(self) -> Frame:
        """On a contact's page, click Edit and return the form's frame."""
        self._close_stale_form()
        button = self.page.get_by_role("button", name=EDIT_BUTTON, exact=True)
        if not button.count():
            button = self.page.get_by_text(EDIT_BUTTON, exact=True)
        try:
            button.first.click(timeout=self.timeout_ms)
        except PlaywrightError as exc:
            raise TealError(f"Couldn't click '{EDIT_BUTTON}': {_short(exc)}") from exc
        frame = self._find_form_frame(timeout_ms=self.timeout_ms)
        if frame is None:
            raise TealError("The Edit form didn't appear")
        return frame

    def _close_stale_form(self) -> None:
        stale = self._find_form_frame(timeout_ms=0)
        if stale is not None:
            self.cancel(stale)

    def _find_form_frame(self, timeout_ms: int) -> Frame | None:
        # The form lives in an iframe; other iframes (chat widgets etc.) may
        # exist too, so pick the frame that actually contains the form.
        marker = FIELD_PLACEHOLDERS[FORM_MARKER_FIELD]
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            for frame in self.page.frames:
                try:
                    if (frame.get_by_placeholder(marker, exact=True).first.is_visible()
                            and self._save_button(frame).is_visible()):
                        return frame
                except PlaywrightError:
                    continue
            if time.monotonic() >= deadline:
                return None
            self.page.wait_for_timeout(250)

    def _save_button(self, frame: Frame) -> Locator:
        return frame.get_by_role("button", name=SAVE_BUTTON).first

    def fill(self, frame: Frame, values: dict[str, str]) -> list[str]:
        """Type each non-blank value into its box, then check it stuck.

        Returns notes about boxes where Teal reformatted the value (e.g. phone
        numbers); a box left empty is an error.
        """
        notes: list[str] = []
        for field, placeholder in FIELD_PLACEHOLDERS.items():
            value = values.get(field, "")
            if not value:
                continue
            box = frame.get_by_placeholder(placeholder, exact=True).first
            try:
                box.fill(value, timeout=self.timeout_ms)
                actual = box.input_value(timeout=self.timeout_ms)
            except PlaywrightError as exc:
                raise TealError(f"Couldn't fill the '{placeholder}' box: {_short(exc)}") from exc
            if not actual.strip():
                raise TealError(f"The '{placeholder}' box stayed empty after typing {value!r}")
            if actual != value:
                notes.append(f"Teal shows {placeholder} as {actual!r} (typed: {value!r})")
        return notes

    def form_is_open(self, frame: Frame) -> bool:
        if frame.is_detached():
            return False
        try:
            return self._save_button(frame).is_visible()
        except PlaywrightError:
            return False

    def save(self, frame: Frame) -> list[dict]:
        """Click Save; return the id-bearing objects Teal sent back."""
        self.harvest()  # forget earlier responses
        try:
            self._save_button(frame).click(timeout=self.timeout_ms)
        except PlaywrightError as exc:
            raise TealError(f"Couldn't click Save: {_short(exc)}") from exc
        if not self._wait_closed(frame):
            problem = self._form_errors(frame)
            raise SaveUnconfirmed(
                "Clicked Save but the form stayed open" + (f" ({problem})" if problem else "")
            )
        self.page.wait_for_timeout(500)
        return self.harvest()

    def create(self, frame: Frame, first: str, last: str, url: str) -> str | None:
        """Save the filled Add form; return the new contact's id if found."""
        returned = self.save(frame)
        for kind in ("url", "name"):
            ids = {o["id"] for o in returned if match_kind(o, first, last, url) == kind}
            if len(ids) == 1:
                return ids.pop()
        # Not in Teal's reply: find it in the list (Save returns there).
        ids = self.ids_in_list(f"{first} {last}".strip())
        if len(ids) == 1:
            return ids[0]
        for ident in ids:  # several with this name: the one with this LinkedIn
            self.open_record(ident)
            if normalize_linkedin_url(self.read_record().get("url", "")) == url:
                return ident
        return None

    def cancel(self, frame: Frame) -> None:
        """Close the form without saving. Best effort."""
        try:
            if self.form_is_open(frame):
                frame.get_by_role("button", name=CANCEL_BUTTON).first.click(timeout=5_000)
                self._wait_closed(frame, timeout_ms=5_000)
        except PlaywrightError:
            pass
        if not frame.is_detached() and self.form_is_open(frame):
            self.page.keyboard.press("Escape")

    def _wait_closed(self, frame: Frame, timeout_ms: int | None = None) -> bool:
        deadline = time.monotonic() + (timeout_ms or self.timeout_ms) / 1000
        while time.monotonic() < deadline:
            if not self.form_is_open(frame):
                return True
            self.page.wait_for_timeout(250)
        return not self.form_is_open(frame)

    def _form_errors(self, frame: Frame) -> str:
        messages: list[str] = []
        try:
            for alert in frame.get_by_role("alert").all():
                text = alert.inner_text(timeout=1_000).strip()
                if text:
                    messages.append(text)
            for box in frame.locator("[aria-invalid=true]").all():
                messages.append(f"'{box.get_attribute('placeholder') or 'a field'}' is invalid")
        except PlaywrightError:
            pass
        return "; ".join(messages)

    # -- a contact's page -------------------------------------------------------

    def open_record(self, ident: str, reload: bool = False) -> None:
        try:
            if reload or self.current_id() != ident:
                self.page.goto(f"{self.tracker_url}/{ident}", wait_until="domcontentloaded",
                               timeout=self.timeout_ms)
            self._control(*CHOICE_FIELDS["relationship"]).wait_for(
                state="visible", timeout=self.timeout_ms)
            self.page.get_by_text(CONTACT_INFO_SECTION, exact=True).first.wait_for(
                state="visible", timeout=self.timeout_ms)
        except PlaywrightError as exc:
            self._raise_if_signed_out(exc)
            raise TealError(f"Teal contact {ident} didn't open: {_short(exc)}") from exc

    def _control(self, section: str, label: str) -> Locator:
        """The dropdown / date button that follows `label` within `section`."""
        return self.page.locator(
            f"xpath=//*[normalize-space(text())='{section}']"
            f"/following::*[normalize-space(text())='{label}'][1]"
            f"/following::*[self::button or self::select or self::input or @role='combobox'][1]"
        ).first

    def read_record(self) -> dict[str, str]:
        """What the open contact's page shows, keyed like compare.LABELS."""
        try:
            lines = [line.strip() for line in
                     self.page.locator("body").inner_text(timeout=self.timeout_ms).splitlines()
                     if line.strip()]
        except PlaywrightError as exc:
            raise TealError(f"Couldn't read the contact's page: {_short(exc)}") from exc
        record: dict[str, str] = {}

        # Header: name, then "Title at Company", then the Edit (and Delete) buttons,
        # which can come out on one line.
        edit_line = next((i for i, line in enumerate(lines)
                          if re.match(rf"^{EDIT_BUTTON}\b", line)), None)
        if edit_line is not None:
            record["name"] = lines[edit_line - 2] if edit_line >= 2 else ""
            record["headline"] = lines[edit_line - 1] if edit_line >= 1 else ""

        if CONTACT_INFO_SECTION in lines:
            section = lines[lines.index(CONTACT_INFO_SECTION) + 1:]
            for field, label in CONTACT_INFO_LABELS.items():
                if label in section:
                    j = section.index(label)
                    value = section[j + 1] if j + 1 < len(section) else ""
                    record[field] = "" if value in ("-", "–", "—") or \
                        value in CONTACT_INFO_LABELS.values() else value

        for field in CHOICE_FIELDS:
            record[field] = self.read_choice(field)
        for field in DATE_FIELDS:
            record[field] = self.read_date(field)
        return record

    def read_choice(self, field: str) -> str:
        control = self._control(*CHOICE_FIELDS[field])
        try:
            if control.evaluate("e => e.tagName") == "SELECT":
                text = control.evaluate("e => e.options[e.selectedIndex]?.text || ''").strip()
            else:
                text = control.inner_text(timeout=self.timeout_ms).strip()
        except PlaywrightError:
            return ""
        # An empty dropdown shows a placeholder such as "Select...".
        if re.match(r"^(select|choose)\b", text, re.I) and not pick_option(text, CHOICE_OPTIONS[field]):
            return ""
        return text

    def read_date(self, field: str) -> str:
        control = self._control(*DATE_FIELDS[field])
        try:
            text = (control.input_value(timeout=2_000)
                    if control.evaluate("e => e.tagName") == "INPUT"
                    else control.inner_text(timeout=self.timeout_ms))
            found = re.search(r"\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{1,2}-\d{1,2}", text)
            parsed = parse_date(found.group(0)) if found else None
        except (PlaywrightError, ValueError):
            return ""
        return parsed.isoformat() if parsed else ""

    def set_choice(self, field: str, value: str) -> str:
        """Pick `value` in a dropdown; returns the option's exact label."""
        section, label = CHOICE_FIELDS[field]
        control = self._control(section, label)
        try:
            if control.evaluate("e => e.tagName") == "SELECT":
                options = [o.strip() for o in control.locator("option").all_inner_texts()]
                match = pick_option(value, options)
                if not match:
                    raise NotAnOption(f"{label}: {value!r} isn't one of Teal's options "
                                      f"({', '.join(o for o in options if o)})")
                control.select_option(label=match, timeout=self.timeout_ms)
                return match
            control.click(timeout=self.timeout_ms)
            options = self.page.get_by_role("option")
            options.first.wait_for(state="visible", timeout=self.timeout_ms)
            texts = [t.strip() for t in options.all_inner_texts()]
            match = pick_option(value, texts)
            if not match:
                self.page.keyboard.press("Escape")
                raise NotAnOption(f"{label}: {value!r} isn't one of Teal's options "
                                  f"({', '.join(texts)})")
            options.nth(texts.index(match)).click(timeout=self.timeout_ms)
            return match
        except PlaywrightError as exc:
            self.page.keyboard.press("Escape")
            raise TealError(f"Couldn't set {label}: {_short(exc)}") from exc

    def set_date(self, field: str, iso: str) -> None:
        """Pick a date in a calendar-only date field.

        The field is a button ("Add a date" or MM/DD/YYYY) that opens a calendar
        popup; the button's aria-controls names the popup, so everything below
        looks only inside it.
        """
        section, label = DATE_FIELDS[field]
        target = date.fromisoformat(iso)
        control = self._control(section, label)
        popup = None
        try:
            control.click(timeout=self.timeout_ms)
            popup = self._calendar_popup(control)
            for _ in range(240):  # up to 20 years either way
                shown = self._shown_month(popup)
                steps = (target.year - shown[0]) * 12 + target.month - shown[1]
                if steps == 0:
                    break
                self._month_button(popup, forward=steps > 0).click(timeout=self.timeout_ms)
                self._wait_month_change(popup, shown)
            else:
                raise TealError(f"Couldn't reach {target:%B %Y} in the {label} calendar")
            self._day_button(popup, target).click(timeout=self.timeout_ms)
            self.page.wait_for_timeout(300)
            if popup.is_visible():
                self.page.keyboard.press("Escape")
        except (PlaywrightError, TealError) as exc:
            self._save_debug_html(f"calendar-{field}", popup)
            self.page.keyboard.press("Escape")
            if isinstance(exc, TealError):
                raise
            raise TealError(f"Couldn't set {label}: {_short(exc)}") from exc

    def _calendar_popup(self, control: Locator) -> Locator:
        popup_id = control.get_attribute("aria-controls", timeout=self.timeout_ms)
        if popup_id:
            popup = self.page.locator(f'[id="{popup_id}"]')
        else:  # no link to the popup: the open dialog that has a calendar grid
            popup = self.page.locator("[role=dialog]").filter(
                has=self.page.locator("table, [role=grid]")).last
        popup.locator("table, [role=grid]").first.wait_for(state="visible", timeout=self.timeout_ms)
        return popup

    def _shown_month(self, popup: Locator) -> tuple[int, int]:
        # The month heading ("October 2026") is the first month name in the popup.
        text = popup.inner_text(timeout=self.timeout_ms)
        found = MONTH_YEAR.search(text)
        if not found:
            raise TealError("Couldn't tell which month the calendar shows")
        return int(found.group(2)), MONTHS.index(found.group(1).title()) + 1

    def _month_button(self, popup: Locator, forward: bool) -> Locator:
        named = popup.get_by_role("button", name=re.compile(r"next" if forward else r"prev", re.I))
        if named.count():
            return named.first
        # Unlabelled arrow buttons: the icon-only ones, previous then next.
        icons = [b for b in popup.locator("button").all() if not b.inner_text().strip()]
        if len(icons) < 2:
            raise TealError("Couldn't find the calendar's month arrows")
        return icons[-1] if forward else icons[0]

    def _wait_month_change(self, popup: Locator, shown: tuple[int, int]) -> None:
        for _ in range(40):
            if self._shown_month(popup) != shown:
                return
            self.page.wait_for_timeout(50)

    def _day_button(self, popup: Locator, target: date) -> Locator:
        # Newer calendars label each day with its date; use that when present.
        for selector in (f'[data-day="{target.isoformat()}"]',
                         f'[data-day="{target.month}/{target.day}/{target.year}"]',
                         f'[data-day="{target:%m/%d/%Y}"]'):
            cell = popup.locator(selector)
            if cell.count():
                button = cell.first.locator("button")
                return button.first if button.count() else cell.first
        labelled = popup.locator("button[aria-label]").all()
        month = MONTHS[target.month - 1]
        pattern = re.compile(rf"\b{month}\s+{target.day}(st|nd|rd|th)?\b.*\b{target.year}\b", re.I)
        for button in labelled:
            if pattern.search(button.get_attribute("aria-label") or ""):
                return button

        # Otherwise by the number shown, skipping the faded days of the months
        # either side.
        candidates = popup.locator("button, [role=gridcell]").filter(
            has_text=re.compile(rf"^\s*{target.day}\s*$"))
        inside = []
        for i in range(candidates.count()):
            cell = candidates.nth(i)
            outside = cell.evaluate(OUTSIDE_DAY_JS)
            if not outside:
                inside.append(cell)
        if not inside:
            raise TealError(f"Couldn't find day {target.day} in the calendar")
        # If outside days can't be told apart: early days come first, late days last.
        return inside[0] if target.day < 15 else inside[-1]

    def _save_debug_html(self, name: str, locator: Locator | None) -> None:
        """Keep the HTML of a part of the page that didn't behave, for diagnosis."""
        if not self.debug_dir:
            return
        try:
            html = locator.evaluate("e => e.outerHTML") if locator is not None and locator.count() \
                else self.page.content()
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            (self.debug_dir / f"{name}-{int(time.time())}.html").write_text(html, encoding="utf-8")
        except Exception:
            pass

    def settle(self, ms: int = 1500) -> None:
        """Give Teal time to save changes made on the page (it saves by itself)."""
        self.page.wait_for_timeout(ms)

    def screenshot(self, path: Path) -> Path | None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(path), timeout=10_000)
            return path
        except PlaywrightError:
            return None


def _short(exc: Exception) -> str:
    return str(exc).strip().splitlines()[0][:200]
