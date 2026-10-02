"""Drive Teal's "Add a New Contact" form on app.tealhq.com/contact-tracker.

If Teal changes its page, the strings below are the first thing to update.
Fields are found by their placeholder text, which is what's shown greyed out
inside each empty box.
"""

from __future__ import annotations

import time
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Frame, Page

TRACKER_URL = "https://app.tealhq.com/contact-tracker"
ADD_BUTTON = "Add a New Contact"  # button on the tracker page
SAVE_BUTTON = "Save Contact"      # buttons inside the form
CANCEL_BUTTON = "Cancel"

# Contact field -> placeholder of the matching box in Teal's form.
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


class TealError(Exception):
    """Something went wrong before Save was clicked; safe to retry."""


class NotLoggedIn(TealError):
    """Teal showed something other than the contact tracker (e.g. a sign-in page)."""


class SaveUnconfirmed(Exception):
    """Save was clicked but the form didn't close. Not safe to retry blindly:
    the contact may have been created."""


class TealPage:
    def __init__(self, page: Page, tracker_url: str = TRACKER_URL, timeout_ms: int = 20_000):
        self.page = page
        self.tracker_url = tracker_url
        self.timeout_ms = timeout_ms

    # -- tracker page -------------------------------------------------------

    def open_tracker(self, reload: bool = False) -> None:
        """Make sure the contact tracker is showing with its Add button."""
        try:
            if reload or not self.page.url.startswith(self.tracker_url):
                self.page.goto(self.tracker_url, wait_until="domcontentloaded",
                               timeout=self.timeout_ms)
            self._add_button().wait_for(state="visible", timeout=self.timeout_ms)
        except PlaywrightError as exc:
            url = self.page.url
            if not url.startswith(self.tracker_url):
                raise NotLoggedIn(f"Teal opened {url} instead of the contact tracker") from exc
            raise TealError(f"The '{ADD_BUTTON}' button didn't appear: {_short(exc)}") from exc

    def _add_button(self):
        return self.page.get_by_role("button", name=ADD_BUTTON).first

    # -- the form -----------------------------------------------------------

    def open_form(self) -> Frame:
        """Click "Add a New Contact" and return the frame holding the form."""
        stale = self._find_form_frame(timeout_ms=0)
        if stale is not None:
            self.cancel(stale)
        try:
            self._add_button().click(timeout=self.timeout_ms)
        except PlaywrightError as exc:
            raise TealError(f"Couldn't click '{ADD_BUTTON}': {_short(exc)}") from exc
        frame = self._find_form_frame(timeout_ms=self.timeout_ms)
        if frame is None:
            raise TealError("The Add a New Contact form didn't appear")
        return frame

    def _find_form_frame(self, timeout_ms: int) -> Frame | None:
        # The form lives in an iframe; other iframes (chat widgets etc.) may
        # exist too, so pick the frame that actually contains the form.
        marker = FIELD_PLACEHOLDERS[FORM_MARKER_FIELD]
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            for frame in self.page.frames:
                try:
                    if (frame.get_by_placeholder(marker, exact=True).first.is_visible()
                            and frame.get_by_role("button", name=SAVE_BUTTON).first.is_visible()):
                        return frame
                except PlaywrightError:
                    continue
            if time.monotonic() >= deadline:
                return None
            self.page.wait_for_timeout(250)

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
            return frame.get_by_role("button", name=SAVE_BUTTON).first.is_visible()
        except PlaywrightError:
            return False

    def save(self, frame: Frame) -> None:
        try:
            frame.get_by_role("button", name=SAVE_BUTTON).first.click(timeout=self.timeout_ms)
        except PlaywrightError as exc:
            raise TealError(f"Couldn't click '{SAVE_BUTTON}': {_short(exc)}") from exc
        if not self._wait_closed(frame):
            problem = self._form_errors(frame)
            raise SaveUnconfirmed(
                "Clicked Save Contact but the form stayed open"
                + (f" ({problem})" if problem else "")
            )

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

    def screenshot(self, path: Path) -> Path | None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(path))
            return path
        except PlaywrightError:
            return None


def _short(exc: Exception) -> str:
    return str(exc).strip().splitlines()[0][:200]
