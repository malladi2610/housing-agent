"""WG-Gesucht messaging through a dedicated Chrome profile (Playwright).

Log in once with `homeward login`; the session is kept in data/browser.
"""
from __future__ import annotations

import re

from playwright.sync_api import TimeoutError as PWTimeout, sync_playwright

from .core import DATA, Listing

PROFILE = DATA / "browser"
LOGIN_URL = "https://www.wg-gesucht.de/mein-wg-gesucht.html"


class WggError(RuntimeError):
    pass


def _context(p, headless: bool):
    PROFILE.mkdir(parents=True, exist_ok=True)
    return p.chromium.launch_persistent_context(
        str(PROFILE), channel="chrome", headless=headless, locale="de-DE",
        viewport={"width": 1280, "height": 900},
    )


def login() -> None:
    with sync_playwright() as p:
        ctx = _context(p, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(LOGIN_URL)
        print("Log in to WG-Gesucht in the opened window, then close the window.")
        try:
            page.wait_for_event("close", timeout=0)
        except Exception:
            pass
        ctx.close()


def message_url(l: Listing) -> str:
    return re.sub(r"wg-gesucht\.de/", "wg-gesucht.de/nachricht-senden/", l.url, count=1)


def send(l: Listing, message: str, headless: bool = True) -> None:
    with sync_playwright() as p:
        ctx = _context(p, headless)
        try:
            page = ctx.new_page()
            page.goto(message_url(l), wait_until="domcontentloaded")
            _dismiss_cookies(page)
            if page.locator("text=/loggen Sie sich/").count() or "login" in page.url:
                raise WggError("not logged in - run `homeward login`")
            if page.locator("text=bereits eine Nachricht").count():
                raise WggError("already contacted")
            # security-advice overlay shown before the first message
            for label in ("Ich habe die Sicherheitstipps gelesen", "Sicherheitstipps gelesen"):
                btn = page.get_by_text(label)
                if btn.count():
                    btn.first.click()
            box = page.locator("textarea#message_input, textarea[name='content'], textarea").first
            box.wait_for(state="visible", timeout=15000)
            box.fill(message)
            page.locator("button:has-text('Nachricht senden'), button[type='submit']").first.click()
            try:
                page.wait_for_selector("text=/erfolgreich|gesendet|Nachricht wurde/i", timeout=20000)
            except PWTimeout:
                raise WggError("no confirmation after send (uncertain)")
        finally:
            ctx.close()


def _dismiss_cookies(page) -> None:
    for label in ("Nur notwendige", "Ablehnen", "Alle ablehnen"):
        btn = page.get_by_role("button", name=label)
        if btn.count():
            btn.first.click()
            return
