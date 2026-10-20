"""Owner-calibrated Android UI contract, shared by Windows and macOS."""
from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import Field, model_validator

from .models import StrictModel


class Selector(StrictModel):
    using: Literal["id", "accessibility id", "xpath"] = "id"
    value: str = Field(min_length=1, max_length=1500)


SCAN_ROLES = {"feed", "cards", "card_identity", "detail", "title", "body"}
SEND_ROLES = {"open_conversation", "thread_identity", "composer", "send", "outgoing"}
OPTIONAL_ROLES = {"contact", "incoming", "refresh", "challenge"}


class AndroidConfig(StrictModel):
    enabled: bool = False
    mode: Literal["review", "automatic"] = "review"
    udid: str = Field(default="", max_length=200, pattern=r"^[a-zA-Z0-9._:\-]*$")
    package: str = Field(default="", max_length=200, pattern=r"^[a-zA-Z0-9._]*$")
    activity: str = Field(default="", max_length=300, pattern=r"^[a-zA-Z0-9._/$]*$")
    poll_seconds: int = Field(default=180, ge=60, le=3600)
    max_pages: int = Field(default=2, ge=1, le=5)
    max_cards: int = Field(default=4, ge=1, le=12)
    detail_swipes: int = Field(default=0, ge=0, le=3)
    refresh: Literal["pull", "button", "none"] = "pull"
    identity_attribute: Literal["text", "content-desc"] = "text"
    thread_matches: Literal["title", "contact"] = "title"
    daily_sends: int = Field(default=5, ge=1, le=30)
    auto_followups: bool = False
    selectors: dict[str, Selector] = Field(default_factory=dict)

    @model_validator(mode="after")
    def valid_roles(self):
        unknown = set(self.selectors) - SCAN_ROLES - SEND_ROLES - OPTIONAL_ROLES
        if unknown:
            raise ValueError("Unknown Android selector roles: " + ", ".join(sorted(unknown)))
        if self.enabled:
            self.require_scan()
        if self.auto_followups and "incoming" not in self.selectors:
            raise ValueError("Automatic follow-ups require a calibrated incoming-message selector")
        return self

    def require_scan(self):
        if not self.udid or not self.package or not self.activity:
            raise ValueError("Select the phone and provide the verified app package and activity")
        missing = SCAN_ROLES - set(self.selectors)
        if missing:
            raise ValueError("Calibrate Android selectors: " + ", ".join(sorted(missing)))
        if self.refresh == "button" and "refresh" not in self.selectors:
            raise ValueError("Button refresh needs a refresh selector")
        identity = self.selectors.get("card_identity")
        if identity and identity.using == "xpath" and not identity.value.lstrip().startswith("."):
            raise ValueError("The card identity XPath must be relative to its own card (start with .)")

    def require_send(self):
        self.require_scan()
        missing = SEND_ROLES - set(self.selectors)
        if missing:
            raise ValueError("Calibrate conversation selectors: " + ", ".join(sorted(missing)))
        if self.thread_matches == "contact" and "contact" not in self.selectors:
            raise ValueError("Contact matching needs a detail-screen contact selector")

    def fingerprint(self):
        # An enabled/mode toggle does not invalidate a completed calibration pilot.
        values = self.model_dump(exclude={"enabled", "mode", "daily_sends", "auto_followups", "poll_seconds"})
        return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def load_config(store) -> AndroidConfig:
    return AndroidConfig.model_validate(store.setting("android_config", {}))


def save_config(store, config: AndroidConfig, *, acknowledged=False):
    previous = load_config(store)
    if config.enabled and config.mode == "automatic":
        config.require_send()
        pilot = store.setting("android_pilot", {})
        if pilot.get("fingerprint") != config.fingerprint() or not pilot.get("conversation_verified"):
            raise ValueError("Run a successful conversation pilot with these settings before enabling automatic sending")
        if not acknowledged:
            raise ValueError("Acknowledge the calibrated identity and automatic enquiry settings")
    with store.connect() as conn:
        conn.execute("INSERT INTO settings VALUES('android_config',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (config.model_dump_json(),))
        # A queued approval belongs to one exact configuration; revisions matter too.
        row = conn.execute("SELECT value FROM settings WHERE key='android_revision'").fetchone()
        revision = int(json.loads(row[0])) + 1 if row else 1
        conn.execute("INSERT INTO settings VALUES('android_revision',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(revision),))
        store._event(conn, None, "android_configured", f"Android monitoring {'enabled' if config.enabled else 'paused'}; {config.mode} mode")
    return previous
