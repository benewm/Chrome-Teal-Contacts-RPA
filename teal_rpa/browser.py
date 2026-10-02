"""Start (or reuse) a real Chrome window with a dedicated profile and attach to it.

We launch chrome.exe ourselves with a remote-debugging port and connect with
Playwright over CDP, instead of letting Playwright launch Chrome. That way
Chrome runs without automation flags, so signing in to Teal (including "Sign
in with Google") works normally, and the window stays open between runs.

Chrome 136+ refuses remote debugging on your everyday profile folder, so the
tool uses its own folder (DEFAULT_PROFILE_DIR). You sign in to Teal there once.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

DEFAULT_PORT = 9333


def default_profile_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path.home() / ".local" / "share"
    return base / "TealContactsRPA" / "chrome-profile"


class BrowserError(Exception):
    pass


def find_chrome() -> str:
    candidates: list[str | None] = []
    if sys.platform == "win32":
        for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            root = os.environ.get(env)
            if root:
                candidates.append(os.path.join(root, "Google", "Chrome", "Application", "chrome.exe"))
    elif sys.platform == "darwin":
        candidates.append("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    for name in ("google-chrome", "google-chrome-stable", "chrome", "chromium", "chromium-browser"):
        candidates.append(shutil.which(name))
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    raise BrowserError("Couldn't find Google Chrome. Pass its location with --chrome-path.")


def _debugger_alive(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1) as resp:
            json.load(resp)
            return True
    except (OSError, ValueError):
        return False


def ensure_chrome(*, chrome_path: str | None, profile_dir: Path, port: int,
                  start_url: str, extra_args: list[str] | None = None,
                  timeout: float = 30) -> str:
    """Make sure a debuggable Chrome is running; return its CDP endpoint URL."""
    endpoint = f"http://127.0.0.1:{port}"
    if _debugger_alive(port):
        return endpoint  # reuse the window from a previous run

    profile_dir.mkdir(parents=True, exist_ok=True)
    args = [
        chrome_path or find_chrome(),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        *(extra_args or []),
        start_url,
    ]
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _debugger_alive(port):
            return endpoint
        time.sleep(0.5)
    raise BrowserError(
        "Chrome started but didn't open its debugging port. This usually means a Chrome "
        f"window using the tool's profile ({profile_dir}) is already open from an earlier, "
        "non-tool launch. Close those Chrome windows and try again."
    )


def connect(playwright, endpoint: str):
    """Attach to Chrome; return (browser, context) using the profile's own context."""
    browser = playwright.chromium.connect_over_cdp(endpoint)
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    return browser, context
