"""Runs the real tool against a local stand-in for Teal's contact tracker.

Needs Playwright and a Chrome/Chromium binary. Set TEAL_RPA_TEST_CHROME to the
browser's path if it isn't found automatically; otherwise these tests skip.
"""

import json
import os
import socket
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

playwright_sync = pytest.importorskip("playwright.sync_api")

from teal_rpa import browser  # noqa: E402
from teal_rpa.runner import Options, Prompter, Run  # noqa: E402
from teal_rpa.spreadsheet import load_contacts  # noqa: E402
from teal_rpa.state import DONE, FAILED, SKIPPED, RunState  # noqa: E402
from teal_rpa.teal import TealPage  # noqa: E402

from .test_spreadsheet import TEAL_HEADERS, make_xlsx, teal_row  # noqa: E402

FAKE_SITE = Path(__file__).parent / "fake_teal"


def _chrome_path():
    for path in (os.environ.get("TEAL_RPA_TEST_CHROME"), "/opt/pw-browsers/chromium"):
        if path and os.path.exists(path):
            return path
    try:
        return browser.find_chrome()
    except browser.BrowserError:
        return None


CHROME = _chrome_path()
pytestmark = pytest.mark.skipif(CHROME is None, reason="no Chrome/Chromium available")


class FakeTeal:
    def __init__(self):
        self.saved = []
        self.logged_in = True


def _handler(fake, *args, **kwargs):
    class Handler(SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/contact-tracker"):
                if not fake.logged_in:
                    self.send_response(302)
                    self.send_header("Location", "/sign-in.html")
                    self.end_headers()
                    return
                self.path = "/contact-tracker.html"
            return super().do_GET()

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            fake.saved.append(json.loads(body))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *a):
            pass

    return Handler(*args, directory=str(FAKE_SITE), **kwargs)


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def site():
    fake = FakeTeal()
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_handler, fake))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    fake.url = f"http://127.0.0.1:{server.server_address[1]}/contact-tracker"
    yield fake
    server.shutdown()


@pytest.fixture(scope="module")
def chrome_page(site, tmp_path_factory):
    from playwright.sync_api import sync_playwright

    port = _free_port()
    endpoint = browser.ensure_chrome(
        chrome_path=CHROME, profile_dir=tmp_path_factory.mktemp("profile"), port=port,
        start_url="about:blank",
        extra_args=["--headless=new", "--no-sandbox", "--disable-gpu"],
    )
    with sync_playwright() as pw:
        b, context = browser.connect(pw, endpoint)
        page = context.new_page()
        yield page
        b.close()


@pytest.fixture
def fresh(site, chrome_page):
    site.saved.clear()
    site.logged_in = True
    chrome_page.goto("about:blank")
    return site, chrome_page


class ScriptedPrompter(Prompter):
    def __init__(self, answers):
        self.questions = []
        answers = list(answers)

        def fake_input(question):
            self.questions.append(question)
            return answers.pop(0)

        super().__init__(fake_input)


def make_run(tmp_path, site, page, rows, answers=(), **options):
    path = make_xlsx(tmp_path / "contacts.xlsx", TEAL_HEADERS, rows)
    sheet = load_contacts(path)
    run = Run(
        sheet=sheet, state=RunState.for_spreadsheet(path),
        teal=TealPage(page, site.url, timeout_ms=5_000),
        options=Options(delay=0, backoff=0.1, **options),
        prompter=ScriptedPrompter(answers), failures_dir=tmp_path / "failures",
        sleep=lambda s: None,
    )
    return run, sheet


def test_saves_contacts_and_writes_back_answers(tmp_path, fresh):
    site, page = fresh
    run, sheet = make_run(tmp_path, site, page, [
        teal_row("Ed", "Soo Hoo", "https://www.linkedin.com/in/edsoohoo",
                 position="WW CTO Global Accounts", company="Lenovo"),
        teal_row("Jane", "Doe", "https://www.linkedin.com/in/jane", email="jane@x.com",
                 phone=5550100123),
    ], answers=["not-an-email", "ed@lenovo.com", "", ""], auto_save=True)

    summary = run.run(sheet.contacts)

    assert [c.name for c in summary.done] == ["Ed Soo Hoo", "Jane Doe"]
    assert site.saved[0] == {
        "first_name": "Ed", "last_name": "Soo Hoo", "title": "WW CTO Global Accounts",
        "company": "Lenovo", "email": "ed@lenovo.com",
        "url": "https://www.linkedin.com/in/edsoohoo", "twitter": "", "location": "",
        "phone": "",
    }
    assert site.saved[1]["phone"] == "(555) 010-0123"  # reformatted by the form, still saved
    # Asked for Ed's email (rejecting the bad one) and phone; nothing for Jane.
    assert len(run.prompter.questions) == 3
    assert "Ed Soo Hoo (WW CTO Global Accounts @ Lenovo)" in run.prompter.questions[0]
    assert load_contacts(sheet.path).contacts[0].email == "ed@lenovo.com"

    state = RunState.for_spreadsheet(sheet.path)
    assert state.is_done("https://www.linkedin.com/in/edsoohoo")
    assert state.recall("https://www.linkedin.com/in/edsoohoo", "phone_skipped") is True


def test_review_pause_skip_and_quit(tmp_path, fresh):
    site, page = fresh
    run, sheet = make_run(tmp_path, site, page, [
        teal_row("A", "One", "https://www.linkedin.com/in/a", email="a@x.com", phone="1"),
        teal_row("B", "Two", "https://www.linkedin.com/in/b", email="b@x.com", phone="2"),
        teal_row("C", "Three", "https://www.linkedin.com/in/c", email="c@x.com", phone="3"),
    ], answers=["", "s", "q"])

    summary = run.run(sheet.contacts)

    assert [c.name for c in summary.done] == ["A One"]
    assert [c.name for c, _ in summary.skipped] == ["B Two"]
    assert summary.not_reached == 1
    assert [s["first_name"] for s in site.saved] == ["A"]
    assert run.state.status("https://www.linkedin.com/in/b") == SKIPPED
    assert run.state.status("https://www.linkedin.com/in/c") == "pending"


def test_dry_run_never_saves_or_records(tmp_path, fresh):
    site, page = fresh
    run, sheet = make_run(tmp_path, site, page, [
        teal_row("A", "One", "https://www.linkedin.com/in/a", email="a@x.com", phone="1"),
    ], dry_run=True)

    summary = run.run(sheet.contacts)

    assert len(summary.dry_run) == 1
    assert site.saved == []
    assert not run.state.path.exists()


def test_validation_error_marks_failed_with_screenshot(tmp_path, fresh):
    site, page = fresh
    run, sheet = make_run(tmp_path, site, page, [
        teal_row("Bad", "Email", "https://www.linkedin.com/in/bad", email="invalid@x", phone="1"),
    ], auto_save=True)

    summary = run.run(sheet.contacts)

    [(contact, reason)] = summary.failed
    assert "Please enter a valid email" in reason
    assert "check Teal before re-running" in reason
    assert (tmp_path / "failures" / "row-2.png").exists()
    assert run.state.status("https://www.linkedin.com/in/bad") == FAILED


def test_rerun_skips_done_rows(tmp_path, fresh):
    site, page = fresh
    rows = [teal_row("A", "One", "https://www.linkedin.com/in/a", email="a@x.com", phone="1")]
    run, sheet = make_run(tmp_path, site, page, rows, auto_save=True)
    run.run(sheet.contacts)
    assert run.state.status("https://www.linkedin.com/in/a") == DONE

    state = RunState.for_spreadsheet(sheet.path)
    todo = [c for c in load_contacts(sheet.path).contacts if not state.is_done(c.key)]
    assert todo == []


def test_waits_for_sign_in(tmp_path, fresh):
    site, page = fresh
    site.logged_in = False
    run, sheet = make_run(tmp_path, site, page, [
        teal_row("A", "One", "https://www.linkedin.com/in/a", email="a@x.com", phone="1"),
    ], auto_save=True)

    def sign_in(question):
        site.logged_in = True
        return ""

    run.prompter = Prompter(sign_in)
    summary = run.run(sheet.contacts)

    assert len(summary.done) == 1


def test_form_that_never_opens_fails_after_retries(tmp_path, fresh, monkeypatch):
    site, page = fresh
    run, sheet = make_run(tmp_path, site, page, [
        teal_row("A", "One", "https://www.linkedin.com/in/a", email="a@x.com", phone="1"),
    ], auto_save=True)
    run.teal.tracker_url = site.url + "?slow=1"
    run.teal.timeout_ms = 1_000  # the form takes 1.5s to open, so it "never" shows
    attempts = []
    original = run.teal.open_form
    monkeypatch.setattr(run.teal, "open_form", lambda: attempts.append(1) or original())

    summary = run.run(sheet.contacts)

    assert len(attempts) == 3  # first try + 2 retries
    [(_, reason)] = summary.failed
    assert "didn't appear" in reason
    assert site.saved == []


def test_input_closing_stops_cleanly(tmp_path, fresh):
    site, page = fresh
    run, sheet = make_run(tmp_path, site, page, [
        teal_row("A", "One", "https://www.linkedin.com/in/a", email="a@x.com", phone="1"),
        teal_row("B", "Two", "https://www.linkedin.com/in/b", email="b@x.com", phone="2"),
    ], answers=[""])  # saves A, then input runs out at B's review

    def scripted(question, answers=[""]):
        if not answers:
            raise EOFError
        return answers.pop()

    run.prompter = Prompter(scripted)
    summary = run.run(sheet.contacts)

    assert [c.name for c in summary.done] == ["A One"]
    assert summary.not_reached == 1
    assert run.state.status("https://www.linkedin.com/in/b") == "pending"
