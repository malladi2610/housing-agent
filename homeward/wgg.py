"""WG-Gesucht messaging through a dedicated, always-running Chrome window.

WG-Gesucht keeps the login in a session cookie, so the bot never quits that window: it runs a
plain Chrome (no automation flags, so Google sign-in works) on its own profile with a
localhost-only debugging port, and Playwright attaches to it to send messages.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx
from playwright.sync_api import TimeoutError as PWTimeout, sync_playwright

from .core import DATA, Listing

PROFILE = DATA / "browser"
PORT = 9333
LOGIN_URL = "https://www.wg-gesucht.de/mein-wg-gesucht.html"


class WggError(RuntimeError):
    pass


def chrome_path() -> str:
    """The installed Google Chrome (the bot uses the real Chrome, not Playwright's bundled browser)."""
    if sys.platform == "darwin":
        candidates = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    elif sys.platform == "win32":  # per-machine install, or per-user install under LOCALAPPDATA
        candidates = [os.path.join(os.environ.get(v, ""), r"Google\Chrome\Application\chrome.exe")
                      for v in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
    else:
        candidates = [shutil.which(n) or "" for n in ("google-chrome", "google-chrome-stable", "chromium")]
    for c in candidates:
        if c and Path(c).is_file():
            return c
    raise WggError("Google Chrome not found - install it from google.com/chrome")


def ensure_browser() -> str:
    """Start the bot's Chrome if it isn't running; return its CDP endpoint."""
    url = f"http://127.0.0.1:{PORT}"
    try:
        httpx.get(url + "/json/version", timeout=2)
        return url
    except httpx.HTTPError:
        pass
    PROFILE.mkdir(parents=True, exist_ok=True)
    subprocess.Popen([chrome_path(), f"--user-data-dir={PROFILE}", f"--remote-debugging-port={PORT}",
                      "--remote-debugging-address=127.0.0.1", "--no-first-run", "--no-default-browser-check",
                      LOGIN_URL], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(30):
        time.sleep(1)
        try:
            httpx.get(url + "/json/version", timeout=2)
            return url
        except httpx.HTTPError:
            continue
    raise WggError("bot Chrome did not start")


def logged_in(page) -> bool:
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    return "modal=sign_in" not in page.url


def login() -> None:
    endpoint = ensure_browser()
    with sync_playwright() as p:
        ctx = p.chromium.connect_over_cdp(endpoint).contexts[0]
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(LOGIN_URL)
        page.bring_to_front()
    print("Log in to WG-Gesucht in the bot's Chrome window. Leave that window open (minimise it).")


def message_url(l: Listing) -> str:
    return re.sub(r"wg-gesucht\.de/", "wg-gesucht.de/nachricht-senden/", l.url, count=1)


def send(l: Listing, message: str) -> None:
    with sync_playwright() as p:
        ctx = p.chromium.connect_over_cdp(ensure_browser()).contexts[0]
        page = ctx.new_page()
        try:
            page.goto(message_url(l), wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
            _dismiss_cookies(page)
            if "modal=sign_in" in page.url or page.locator("#login_form:visible").count():
                raise WggError("not logged in - run `homeward login`")
            if page.locator("text=/Anzeige ist bereits gelöscht|Anzeige ist deaktiviert/").count():
                raise WggError("skip: ad deleted")
            if page.locator("text=bereits eine Nachricht").count():
                raise WggError("already contacted")
            if page.locator("text=HousingAnywhere").count():
                raise WggError("skip: agency listing (HousingAnywhere, booking fees)")
            # safety-tips overlay shown before messaging
            tips = page.locator("#sec_advice_submit_button")
            if tips.is_visible():
                tips.click()
                page.wait_for_timeout(500)
            plus = page.locator("#wgg_plus_toggle")  # paid "appear at top of inbox" boost
            if plus.count() and plus.is_checked():
                raise WggError("paid boost toggle is on, not sending")
            box = page.locator("textarea#message_input")
            box.wait_for(state="visible", timeout=15000)
            box.fill(message)
            if box.input_value().strip()[:40] != message.strip()[:40]:
                raise WggError("composer text mismatch, not sending")
            page.locator("button.wgg-btn-primary", has_text=re.compile(r"^\s*Senden\s*$")).first.click()
            try:  # success = we leave the compose page or see a confirmation
                page.wait_for_function(
                    "() => !location.pathname.startsWith('/nachricht-senden') || "
                    "!(document.querySelector('#message_input') || {value: ''}).value || "
                    "/erfolgreich|wurde gesendet|Nachricht gesendet/i.test(document.body.innerText)", timeout=20000)
            except PWTimeout:
                raise WggError("no confirmation after send (uncertain)")
        finally:
            page.close()


INBOX_JS = """() => [...document.querySelectorAll('div[id^=conversation_][data-conversation_id]')].map(e => {
  const lines = e.innerText.split('\\n').map(t => t.trim()).filter(Boolean);
  const name = (e.querySelector('.contacted_user_public_name') || {}).innerText || lines[0] || '';
  const mine = lines.find(t => t.startsWith('Ich:'));
  return {id: e.dataset.conversation_id, last: e.dataset.message_id, name: name.trim(),
          title: lines[1] || '', preview: mine || lines[2] || '', mine: !!mine};
})"""


def inbox() -> list[dict]:
    """Conversation rows of the WG-Gesucht inbox (first page, newest first)."""
    with sync_playwright() as p:
        ctx = p.chromium.connect_over_cdp(ensure_browser()).contexts[0]
        page = ctx.new_page()
        try:
            page.goto("https://www.wg-gesucht.de/nachrichten.html", wait_until="domcontentloaded")
            try:
                page.wait_for_selector("div[id^=conversation_][data-conversation_id]", timeout=20000)
            except PWTimeout:
                pass
            if "modal=sign_in" in page.url:
                raise WggError("not logged in - run `homeward login`")
            return page.evaluate(INBOX_JS)
        finally:
            page.close()


def _dismiss_cookies(page) -> None:
    for label in ("Nur notwendige", "Ablehnen", "Alle ablehnen"):
        btn = page.get_by_role("button", name=label)
        if btn.count():
            btn.first.click()
            return
