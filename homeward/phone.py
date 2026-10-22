"""Flatsforfriendz via the Android app over ADB (uiautomator2).

Flow (calibrated on app v3.8.1, Galaxy S23):
  deep link -> listing screen -> read all text (scrolling) -> "Message <host>"
  -> upsell sheet: always "Without visibility push" (never the paid boost)
  -> EditText composer (max 1000 chars) -> "Confirm & Send request"
"""
from __future__ import annotations

import time
import xml.etree.ElementTree as ET

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
        for _ in range(15):
            if self.d(descriptionStartsWith="Message ").exists:
                return
            if self.d(descriptionMatches="(?i).*already requested.*|.*bereits angefragt.*").exists:
                raise PhoneError("already contacted")
            time.sleep(1)
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

    def tab(self, name: str) -> None:
        """Go to a bottom-navigation tab (Explore, Guide, Home, Chat, Profile)."""
        for _ in range(6):
            if self.d.app_current().get("package") != PKG:  # backed out of the app, or never in it
                self.d.shell(f"am start -W -n {PKG}/.MainActivity")
                time.sleep(2)
            tab = self.d(text=name, className="android.widget.TextView")  # desc gains badges ("Chat, 1")
            if tab.exists:
                tab.click()
                time.sleep(2.5)
                return
            self.d.press("back")
            time.sleep(1)
        raise PhoneError(f"could not reach the {name} tab")

    def chats(self) -> list[dict]:
        """Rows of the Chat tab (newest first): name, time, last-message preview."""
        self.tab("Chat")
        if not self.d(textStartsWith="Chats").wait(timeout=5):
            raise PhoneError("not on the Chats screen")
        if self.d(description="All").click_exists(timeout=3):
            time.sleep(2)
        rows = []
        for node in ET.fromstring(self.d.dump_hierarchy()).iter("node"):
            if node.get("clickable") != "true" or node.get("package") != PKG:
                continue
            texts = [c.get("text", "").strip() for c in node.iter("node")
                     if c.get("class") == "android.widget.TextView" and c.get("text", "").strip()]
            if 3 <= len(texts) <= 5:
                rows.append({"name": texts[0], "when": texts[1], "preview": texts[-1]})
        return rows

    def home(self) -> None:
        self.d.press("home")
