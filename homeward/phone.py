"""Flatsforfriendz via the Android app over ADB (uiautomator2).

Flow (calibrated on app v3.8.1, Galaxy S23):
  deep link -> listing screen -> read all text (scrolling) -> "Message <host>"
  -> upsell sheet: always "Without visibility push" (never the paid boost)
  -> EditText composer (max 1000 chars) -> "Confirm & Send request"
"""
from __future__ import annotations

import time

import uiautomator2 as u2

from .core import Listing

PKG = "com.flatsforfriendz.app"
FREE_OPTION = "Without visibility push"
SEND = "Confirm & Send request"


class PhoneError(RuntimeError):
    pass


class Phone:
    def __init__(self, serial: str = ""):
        self.d = u2.connect(serial or None)

    def ready(self) -> str | None:
        """Return a reason the phone can't be used right now, or None."""
        info = self.d.info
        if not info.get("screenOn"):
            self.d.screen_on()
            time.sleep(1)
        lock = self.d.shell("dumpsys window | grep -iE '(KeyguardShowing|ShowingLockscreen|DreamingLockscreen)=true'")
        if lock.output.strip():
            return "phone is locked"
        return None

    def open(self, l: Listing) -> None:
        self.d.shell(f"am start -W -a android.intent.action.VIEW -d '{l.url}' {PKG}")
        if not self.d(descriptionStartsWith="Message ").wait(timeout=15):
            if self.d(textContains="Request sent").exists or self.d(textContains="Anfrage gesendet").exists:
                raise PhoneError("already contacted")
            raise PhoneError("listing screen did not load (no 'Message' button)")

    def read(self, max_scrolls: int = 8) -> str:
        """Collect all visible text of the listing, scrolling down until nothing new appears."""
        seen: list[str] = []
        for _ in range(max_scrolls):
            new = 0
            for el in self.d.xpath(f'//android.widget.TextView[@package="{PKG}"]').all():
                t = (el.text or "").strip()
                if len(t) > 1 and t not in seen:
                    seen.append(t)
                    new += 1
            if not new:
                break
            self.d.swipe_ext("up", scale=0.6)
            time.sleep(1)
        return "\n".join(seen)

    def host(self) -> str:
        el = self.d(descriptionStartsWith="Message ")
        return el.info["contentDescription"].removeprefix("Message ").strip() if el.exists else ""

    def send(self, message: str) -> None:
        d = self.d
        d(descriptionStartsWith="Message ").click()
        if d(description=FREE_OPTION).wait(timeout=6):
            d(description=FREE_OPTION).click()
        box = d(className="android.widget.EditText")
        if not box.wait(timeout=10):
            raise PhoneError("composer did not open")
        box.set_text(message[:1000])
        time.sleep(0.8)
        if (box.get_text() or "").strip()[:40] != message[:1000].strip()[:40]:
            raise PhoneError("composer text mismatch, not sending")
        d.press("back")  # hide keyboard so the send button is tappable
        time.sleep(0.8)
        btn = d(description=SEND)
        if not btn.exists:
            raise PhoneError("send button not found")
        btn.click()
        # success = composer gone (sheet closes); anything else is surfaced for review
        if not box.wait_gone(timeout=15):
            raise PhoneError("composer still open after send (uncertain)")
        time.sleep(1.5)

    def home(self) -> None:
        self.d.press("home")
