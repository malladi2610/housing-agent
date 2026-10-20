"""Small Appium W3C client. No guessed selectors, coordinates or login handling."""
from __future__ import annotations

import base64
import hashlib
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from urllib.parse import quote

import httpx

from .android_config import AndroidConfig
from .connectors import AndroidInspector

ELEMENT = "element-6066-11e4-a52e-4f735466cecf"


class PhoneUnavailable(RuntimeError):
    pass


class PhoneInUse(PhoneUnavailable):
    pass


class CalibrationRequired(ValueError):
    pass


def normalized(value):
    return " ".join(str(value).split())


def content_hash(title, body, contact=""):
    return hashlib.sha256(json_text(title, body, contact).encode()).hexdigest()


def json_text(*parts):
    import json
    return json.dumps([normalized(p) for p in parts], ensure_ascii=False)


def parse_source(xml):
    if not isinstance(xml, str) or len(xml) > 1_000_000 or "<!DOCTYPE" in xml.upper() or "<!ENTITY" in xml.upper():
        raise CalibrationRequired("Unsupported phone UI source")
    try:
        return ET.fromstring(xml)
    except ET.ParseError:
        raise CalibrationRequired("The phone returned invalid UI source") from None


def source_nodes(xml, package=""):
    root = parse_source(xml)
    result = []
    for node in root.iter():
        a = node.attrib
        if package and a.get("package") not in {package, "", None}:
            continue
        if a.get("text") or a.get("content-desc") or a.get("resource-id"):
            result.append({k: a.get(k, "") for k in ("text", "content-desc", "resource-id", "class", "bounds")})
        if len(result) >= 300:
            break
    return result


def adb_devices():
    binary = shutil.which("adb")
    if not binary:
        return {"available": False, "devices": [], "detail": "Install Android platform tools and restart the services."}
    try:
        result = subprocess.run([binary, "devices", "-l"], capture_output=True, text=True, timeout=12, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return {"available": False, "devices": [], "detail": "ADB did not respond."}
    devices = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[1] in {"device", "offline", "unauthorized", "recovery"}:
            devices.append({"udid": fields[0], "state": fields[1], "model": next((s[6:] for s in fields[2:] if s.startswith("model:")), "")})
    return {"available": result.returncode == 0, "devices": devices, "detail": "Allow USB debugging on your own phone; no wireless ADB is enabled."}


def adb_foreground(udid):
    binary = shutil.which("adb")
    if not binary or not re.fullmatch(r"[a-zA-Z0-9._:\-]{1,200}", udid):
        return {}
    try:
        result = subprocess.run([binary, "-s", udid, "shell", "dumpsys", "activity", "activities"], capture_output=True, text=True, timeout=12, check=False)
        match = re.search(r"(?:mResumedActivity|topResumedActivity)[^\n]*?\s([a-zA-Z0-9_.]+)/([a-zA-Z0-9_.$]+)", result.stdout)
        return {"package": match[1], "activity": match[2]} if match else {}
    except (OSError, subprocess.TimeoutExpired):
        return {}


class AppiumPhone:
    def __init__(self, settings, store, config: AndroidConfig, guard=lambda: None, client=None):
        self.settings, self.store, self.config, self.guard = settings, store, config, guard
        self.base = AndroidInspector(settings.appium_url).base_url
        self.client = client or httpx.Client(timeout=20, trust_env=False)
        self.owns_client = client is None
        self.started = time.monotonic()
        cached = store.setting("android_session", {})
        self.session_signature = hashlib.sha256(json_text(self.base, config.udid, config.package, config.activity).encode()).hexdigest()
        reusable = cached.get("signature") == self.session_signature or (not cached.get("signature") and cached.get("fingerprint") == config.fingerprint())
        self.session = cached.get("id", "") if reusable else ""
        self.previous_session = cached.get("id", "") if not reusable and cached.get("endpoint", self.base) == self.base else ""
        if self.session and not re.fullmatch(r"[a-zA-Z0-9_.-]{1,200}", self.session):
            self.session = ""
        if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,200}", self.previous_session):
            self.previous_session = ""

    def close(self):
        if self.owns_client:
            self.client.close()

    def call(self, method, path, payload=None):
        self.guard()
        if time.monotonic() - self.started > 240:
            raise PhoneUnavailable("Phone task reached its four-minute bound; reduce pages/cards or inspect the connection")
        try:
            response = self.client.request(method, self.base + path, json=payload if method != "GET" else None, timeout=120 if path == "/session" else 20)
            value = response.json().get("value")
            if isinstance(value, dict) and value.get("error"):
                if value["error"] in {"invalid selector", "invalid argument", "unknown command", "unsupported operation", "invalid element state", "element not interactable"}:
                    raise CalibrationRequired("Appium rejected a configured selector or gesture. Check the mapping and installed UiAutomator2 version.")
                raise PhoneUnavailable("Appium could not complete this phone action")
            response.raise_for_status()
            return value
        except CalibrationRequired:
            raise
        except (httpx.HTTPError, ValueError):
            raise PhoneUnavailable("Phone/Appium is unavailable. Check USB authorization, the cable and the local Appium service.") from None

    def connect(self):
        if self.session:
            try:
                self.foreground()
                return
            except PhoneInUse:
                raise
            except PhoneUnavailable:
                self.previous_session = self.session
                self.session = ""
        if self.previous_session:
            # Release only our recorded session, under the device lease. Editing
            # selectors reuses a session; switching phone/app must retire the old one.
            try:
                self.call("DELETE", f"/session/{self.previous_session}")
            except PhoneUnavailable:
                pass
            self.store.set_setting("android_session", {})
            self.previous_session = ""
        caps = {"platformName": "Android", "appium:automationName": "UiAutomator2", "appium:udid": self.config.udid,
                "appium:appPackage": self.config.package, "appium:appActivity": self.config.activity,
                "appium:noReset": True, "appium:autoLaunch": False, "appium:shouldTerminateApp": False,
                "appium:forceAppLaunch": False, "appium:newCommandTimeout": max(600, self.config.poll_seconds * 2)}
        value = self.call("POST", "/session", {"capabilities": {"alwaysMatch": caps}})
        identifier = value.get("sessionId", "") if isinstance(value, dict) else ""
        if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,200}", identifier):
            raise PhoneUnavailable("Appium did not return a usable session ID")
        reported = value.get("capabilities", {}).get("udid") or value.get("capabilities", {}).get("appium:udid")
        if reported and reported != self.config.udid:
            raise CalibrationRequired("Appium selected a different phone; no action was performed")
        self.session = identifier
        self.store.set_setting("android_session", {"id": identifier, "fingerprint": self.config.fingerprint(), "signature": self.session_signature, "endpoint": self.base})
        self.foreground()

    def path(self, suffix):
        return f"/session/{self.session}{suffix}"

    def foreground(self):
        if self.call("GET", self.path("/appium/device/current_package")) != self.config.package:
            raise PhoneInUse("Open the configured rental app on the unlocked phone. Monitoring waits while you use another app.")

    def source(self):
        self.foreground()
        xml = self.call("GET", self.path("/source"))
        root = parse_source(xml)
        if any(n.get("package") == "com.android.systemui" and any(s in n.get("resource-id", "").lower() for s in ("keyguard", "bouncer", "lockscreen")) for n in root.iter()):
            raise PhoneUnavailable("The phone is locked; unlock it yourself to resume monitoring")
        if "challenge" in self.config.selectors and self.find("challenge"):
            raise CalibrationRequired("The app shows the configured login/challenge screen; resolve it yourself before resuming")
        texts = {normalized(n.get("text", "")).casefold() for n in root.iter()}
        if texts.intersection({"captcha", "verify you are human", "verify you're human", "bestätige, dass du ein mensch bist"}):
            raise CalibrationRequired("A login/verification challenge needs your attention")
        return xml

    def find(self, role, parent=None):
        selector = self.config.selectors.get(role)
        if not selector:
            return []
        prefix = f"/element/{quote(parent, safe='')}" if parent else ""
        result = self.call("POST", self.path(prefix + "/elements"), selector.model_dump())
        if not isinstance(result, list) or len(result) > 200:
            raise CalibrationRequired("Selector returned an unsupported number of elements: " + role)
        return [item[ELEMENT] for item in result if isinstance(item, dict) and ELEMENT in item]

    def one(self, role, parent=None):
        found = self.find(role, parent)
        if len(found) != 1:
            raise CalibrationRequired(f"The {role} selector must identify exactly one element")
        return found[0]

    def attr(self, element, name):
        value = self.call("GET", self.path(f"/element/{quote(element, safe='')}/attribute/{name}"))
        return ("true" if value else "false") if isinstance(value, bool) else str(value or "")

    def text(self, element, *, container=False):
        value = str(self.call("GET", self.path(f"/element/{quote(element, safe='')}/text")) or "")
        if value or not container:
            return value
        bounds = self.attr(element, "bounds")
        nodes = [n for n in parse_source(self.source()).iter() if bounds and n.get("bounds") == bounds and n.get("package") == self.config.package]
        if not nodes:
            raise CalibrationRequired("This container exposes no readable app text; choose its text element instead")
        parts = []
        for node in nodes[0].iter():
            part = node.get("text") or node.get("content-desc") or ""
            if part.strip() and part not in parts:
                parts.append(part)
        return "\n".join(parts)

    def tap(self, element):
        self.source()
        if self.attr(element, "enabled") == "false":
            raise CalibrationRequired("The selected app control is disabled")
        self.call("POST", self.path(f"/element/{quote(element, safe='')}/click"), {})

    def scroll(self, direction, *, pull=False):
        self.source()
        rect = self.call("GET", self.path("/window/rect"))
        args = {"left": int(rect["width"] * .1), "top": int(rect["height"] * .25), "width": int(rect["width"] * .8), "height": int(rect["height"] * .55), "direction": direction, "percent": .7}
        return self.call("POST", self.path("/execute/sync"), {"script": "mobile: swipeGesture" if pull else "mobile: scrollGesture", "args": [args]})

    def feed(self):
        self.source()
        for _ in range(3):
            if self.find("feed"):
                return
            if not self.find("detail") and not self.find("composer"):
                raise CalibrationRequired("Open the listing feed. The current screen is outside the calibrated navigation path.")
            self.call("POST", self.path("/back"), {})
            self.source()
        raise CalibrationRequired("Could not return to the listing feed")

    def refresh_feed(self):
        self.feed()
        for _ in range(self.config.max_pages + 1):
            if not self.scroll("up"):
                break
        if self.config.refresh == "pull":
            self.scroll("down", pull=True)
        elif self.config.refresh == "button":
            self.tap(self.one("refresh"))
        self.feed()

    def cards(self):
        self.feed()
        result = {}
        for element in self.find("cards")[:30]:
            identity_element = self.one("card_identity", element)
            identity = self.attr(identity_element, "content-desc") if self.config.identity_attribute == "content-desc" else self.text(identity_element, container=True)
            identity = normalized(identity)
            if not identity or len(identity) > 1000 or identity in result:
                raise CalibrationRequired("Listing identities are empty or duplicated. Choose an immutable, unique listing label/ID before enabling automatic sends.")
            result[identity] = element
        return result

    def open_card(self, identity):
        cards = self.cards()
        if identity not in cards:
            raise PhoneUnavailable("The listing is no longer visible in the bounded feed scan")
        self.tap(cards[identity])
        self.source()
        self.one("detail")

    def locate(self, identity):
        self.refresh_feed()
        for page in range(self.config.max_pages):
            if identity in self.cards():
                self.open_card(identity)
                return
            if page + 1 < self.config.max_pages:
                self.scroll("down")
        raise PhoneUnavailable("This listing is outside the monitored feed pages; open it manually or increase the bounded scan")

    def snapshot(self):
        self.source()
        self.one("detail")
        title = self.text(self.one("title")).strip()
        contact = self.text(self.one("contact")).strip() if "contact" in self.config.selectors else ""
        parts = []
        for page in range(self.config.detail_swipes + 1):
            part = self.text(self.one("body"), container=True).strip()
            if part and part not in parts:
                parts.append(part)
            if page < self.config.detail_swipes:
                self.scroll("down")
        body = "\n".join(parts)
        for _ in range(self.config.detail_swipes):
            self.scroll("up")
        if not title or len(title) > 250 or not body or len(body) > 50000:
            raise CalibrationRequired("The listing title/body could not be read completely within the configured bounds")
        return {"title": title, "body": body, "contact": contact, "fingerprint": content_hash(title, body, contact)}

    def conversation(self, snapshot):
        self.tap(self.one("open_conversation"))
        expected = normalized(snapshot[self.config.thread_matches])
        if not expected:
            raise CalibrationRequired("The expected conversation identity is empty")
        self.thread(expected)
        return expected

    def thread(self, expected):
        self.source()
        actual = normalized(self.text(self.one("thread_identity")))
        if actual != expected:
            raise CalibrationRequired("Conversation identity differs from the listing; nothing was sent")
        self.one("composer")

    def messages(self, expected):
        self.thread(expected)
        incoming, outgoing = self.find("incoming"), self.find("outgoing")
        if set(incoming).intersection(outgoing):
            raise CalibrationRequired("Incoming and outgoing selectors overlap")
        return ([self.text(e) for e in incoming], [self.text(e) for e in outgoing])

    def compose(self, expected, body):
        self.thread(expected)
        element = self.one("composer")
        if self.text(element).strip():
            raise CalibrationRequired("The composer contains an existing draft. Review it on the phone; it was not overwritten.")
        self.call("POST", self.path(f"/element/{quote(element, safe='')}/value"), {"text": body, "value": [body]})
        self.thread(expected)
        if self.text(self.one("composer")) != body:
            raise CalibrationRequired("The app composer does not contain the exact saved message")

    def click_send(self, expected, body, before_click):
        self.thread(expected)
        if self.text(self.one("composer")) != body:
            raise CalibrationRequired("The message changed before send")
        button = self.one("send")
        if self.attr(button, "enabled") == "false":
            raise CalibrationRequired("The app send button is disabled")
        before_click()  # Durable intent and final DB checks happen before the side effect.
        self.call("POST", self.path(f"/element/{quote(button, safe='')}/click"), {})

    def proof(self, expected, body):
        for attempt in range(3):
            self.thread(expected)
            matches = [e for e in self.find("outgoing") if self.text(e) == body]
            if len(matches) == 1:
                return {"thread": expected, "body_sha256": hashlib.sha256(body.encode()).hexdigest(), "outgoing_element": matches[0]}
            if attempt < 2:
                time.sleep(.4)
        raise CalibrationRequired("No unique outgoing message with the exact body was observed; delivery is uncertain and will not be retried automatically")
