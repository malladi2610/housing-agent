"""Bounded app polling, LangChain handoff and evidence-based Android enquiries."""
from __future__ import annotations

import base64
import hashlib
import json
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .android_config import load_config, save_config
from .android_phone import AppiumPhone, CalibrationRequired, PhoneUnavailable
from .android_store import DeviceLedger
from .db import now
from .models import CLOSED_STAGES, ListingInput


class AndroidPipeline:
    def __init__(self, settings, store, phone_factory=AppiumPhone):
        self.settings, self.store, self.phone_factory = settings, store, phone_factory
        self.ledger = DeviceLedger(store)

    @contextmanager
    def phone(self, config):
        revision = self.store.setting("android_revision", 0)
        with self.ledger.lock() as lease_guard:
            def guard():
                lease_guard()
                if self.store.setting("android_revision", 0) != revision:
                    raise ValueError("Phone configuration changed during this task")
            phone = self.phone_factory(self.settings, self.store, config, guard)
            try:
                phone.connect()
                yield phone
            finally:
                phone.close()

    def capture(self):
        config = load_config(self.store)
        if not config.udid or not config.package or not config.activity:
            raise ValueError("Save the phone ID, app package and activity before inspecting it")
        with self.phone(config) as phone:
            xml = phone.source()
            data = base64.b64decode(phone.call("GET", phone.path("/screenshot")), validate=True)
            if len(data) > 5_000_000 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValueError("Unsupported phone screenshot")
            folder = self.settings.data_dir / "screens"
            folder.mkdir(parents=True, exist_ok=True)
            identifier = uuid.uuid4().hex
            (folder / f"{identifier}.xml").write_text(xml, encoding="utf-8")
            (folder / f"{identifier}.png").write_bytes(data)
            self.store.set_setting("latest_screen", identifier)
            self.store.set_setting("android_inspection", identifier)
            self.store.connector("android", "ready", "Phone screenshot and UI nodes saved locally. No screenshot was sent to a model.")
            return identifier

    def import_ad(self, identity, snapshot, config):
        external = "android:" + hashlib.sha256((config.package + "\0" + identity).encode()).hexdigest()
        data = ListingInput(source="flatsforfriendz", external_id=external, title=snapshot["title"], description=snapshot["body"], contact_name=snapshot["contact"])
        identifier, created = self.store.add_listing(data)
        try:
            previous = self.ledger.ad(identifier)
        except ValueError:
            previous = None
        changed = not created and (not previous or previous["fingerprint"] != snapshot["fingerprint"])
        if changed:
            current = self.store.listing(identifier)
            # Changed source text invalidates old price/date/route confirmations.
            self.store.update_listing(identifier, data, current["version"])
            self.store.set_setting(f"manual_fields:{identifier}", [])
            self.store.event(identifier, "android_ad_changed", "Phone listing text changed. Re-verify costs, dates and route facts before another send.")
        self.ledger.save_ad(identifier, identity, snapshot, config)
        if (created or changed) and self.settings.secret("OLLAMA_API_KEY"):
            self.store.enqueue("analyze", identifier, dedupe_key=f"android-analyze:{identifier}:{self.store.listing(identifier)['version']}")
        return identifier

    def pilot(self):
        config = load_config(self.store)
        config.require_scan()
        if config.enabled:
            raise ValueError("Pause phone monitoring before running its calibration pilot")
        with self.phone(config) as phone:
            phone.refresh_feed()
            cards = phone.cards()
            if not cards:
                raise CalibrationRequired("No listing cards are visible. Open a populated feed and check its selectors.")
            identity = next(iter(cards))
            phone.open_card(identity)
            snapshot = phone.snapshot()
            identifier = self.import_ad(identity, snapshot, config)
            verified = False
            if set(config.selectors).issuperset({"open_conversation", "thread_identity", "composer", "send", "outgoing"}):
                config.require_send()
                target = phone.conversation(snapshot)
                phone.messages(target)
                phone.one("composer")
                phone.one("send")
                verified = True
            phone.feed()
            self.store.set_setting("android_pilot", {"fingerprint": config.fingerprint(), "conversation_verified": verified, "listing_id": identifier, "at": now()})
            self.store.event(identifier, "android_pilot", "Opened and re-identified one listing" + (" and its conversation; no text was entered or sent" if verified else "; configure conversation selectors before automatic enquiries"))
            self.store.connector("android", "ready", "Read-only navigation pilot completed. Enable monitoring after checking the captured listing/identity.")
            return identifier

    def queue_ready(self):
        config = load_config(self.store)
        if not config.enabled or not self.settings.secret("OLLAMA_API_KEY"):
            return
        for ad in self.ledger.ads():
            if ad["udid"] != config.udid or ad["package"] != config.package:
                continue
            listing = self.store.listing(ad["listing_id"])
            if listing["stage"] in CLOSED_STAGES or listing["stage"] in {"viewing_proposed", "viewing_confirmed", "viewed"}:
                continue
            if listing["match"]["status"] != "match":
                if self.store.setting(f"android_analysis:{listing['id']}") != listing["version"]:
                    self.store.enqueue("analyze", listing["id"], dedupe_key=f"android-analyze:{listing['id']}:{listing['version']}")
                continue
            messages = self.store.messages(listing["id"])
            if any(m["status"] == "sent" or m["direction"] == "inbound" for m in messages):
                continue
            current = [m for m in messages if m["status"] == "draft" and m["listing_version"] == listing["version"] and m["profile_revision"] == self.store.profile().revision]
            if current:
                self.queue_send(current[-1]["id"])
            else:
                self.store.enqueue("draft", listing["id"], dedupe_key=f"android-draft:{listing['id']}:{listing['version']}:{self.store.profile().revision}")

    def queue_send(self, message_id):
        config = load_config(self.store)
        message = self.store.message(message_id)
        if not config.enabled or config.mode != "automatic" or message["kind"] not in {"initial", "followup"}:
            return
        if message["kind"] == "followup" and not config.auto_followups:
            return
        try:
            self.ledger.ad(message["listing_id"])
        except ValueError:
            return
        self.store.enqueue("android_send", message["listing_id"], {"message_id": message_id, "mode": "automatic"}, dedupe_key=f"android-send:{message_id}:{message['version']}")

    def incoming(self, identifier, phone, target):
        incoming, outgoing = phone.messages(target)
        for index, body in enumerate(incoming):
            if not body.strip():
                continue
            external = "android-reply:" + hashlib.sha256(f"{identifier}\0{index}\0{body}".encode()).hexdigest()
            with self.store.connect() as conn:
                exists = conn.execute("SELECT id FROM messages WHERE external_id=?", (external,)).fetchone()
            if not exists:
                message_id = self.store.add_reply(identifier, body, external_id=external)
                if self.settings.secret("OLLAMA_API_KEY"):
                    self.store.enqueue("reply", identifier, {"message_id": message_id}, dedupe_key=f"reply:{message_id}")
        return incoming, outgoing

    def poll(self):
        config = load_config(self.store)
        if not config.enabled:
            return
        config.require_scan()
        count, seen = 0, set()
        try:
            with self.phone(config) as phone:
                phone.refresh_feed()
                for page in range(config.max_pages):
                    seen.update(phone.cards())
                    if page + 1 >= config.max_pages:
                        break
                    if not phone.scroll("down"):
                        break
                known = {a["card_identity"]: a["last_seen"] for a in self.ledger.ads() if a["udid"] == config.udid and a["package"] == config.package}
                # Read new identities first, then rotate stale ones. A busy first
                # page must not starve unseen cards on the next bounded page.
                candidates = sorted(seen, key=lambda key: (key in known, known.get(key, ""), key))
                for identity in candidates[:config.max_cards]:
                    phone.locate(identity)
                    snapshot = phone.snapshot()
                    self.import_ad(identity, snapshot, config)
                    count += 1
                    phone.feed()
                # Rotate through contacted listings rather than always checking the first.
                if "incoming" in config.selectors and set(config.selectors).issuperset({"open_conversation", "thread_identity", "composer", "outgoing"}):
                    cursor = self.store.setting("android_reply_cursor", 0)
                    contacted = [a for a in self.ledger.ads() if a["udid"] == config.udid and a["package"] == config.package and any(m["status"] == "sent" for m in self.store.messages(a["listing_id"])) and self.store.listing(a["listing_id"])["stage"] not in CLOSED_STAGES]
                    contacted.sort(key=lambda a: (a["listing_id"] <= cursor, a["listing_id"]))
                    for ad in contacted[:2]:
                        self.store.set_setting("android_reply_cursor", ad["listing_id"])
                        try:
                            phone.locate(ad["card_identity"])
                        except PhoneUnavailable:
                            continue
                        snapshot = phone.snapshot()
                        if snapshot["fingerprint"] != ad["fingerprint"]:
                            self.import_ad(ad["card_identity"], snapshot, config)
                        target = phone.conversation(snapshot)
                        self.incoming(ad["listing_id"], phone, target)
                        phone.feed()
                phone.feed()
            self.queue_ready()
            self.store.connector("android", "ready", f"Last feed scan read {count} listing(s). Phone enquiries: {config.mode}; missing facts remain in review.")
        except CalibrationRequired as exc:
            self.pause(str(exc))
            raise
        except PhoneUnavailable as exc:
            self.store.connector("android", "waiting", str(exc))
            return "waiting"

    def pause(self, detail):
        config = load_config(self.store).model_copy(update={"enabled": False})
        save_config(self.store, config)
        self.store.connector("android", "needs_review", detail[:600])

    def defer(self, job, until=None):
        local = datetime.now(ZoneInfo("Europe/Berlin"))
        earliest = local if until is None else until.astimezone(ZoneInfo("Europe/Berlin"))
        profile = self.store.profile()
        if earliest.hour < profile.contact_start_hour:
            earliest = earliest.replace(hour=profile.contact_start_hour, minute=0, second=0, microsecond=0)
        elif earliest.hour >= profile.contact_end_hour:
            earliest = (earliest + timedelta(days=1)).replace(hour=profile.contact_start_hour, minute=0, second=0, microsecond=0)
        with self.store.connect() as conn:
            conn.execute("UPDATE jobs SET status='queued',due_at=?,lease_until=NULL,lease_token=NULL,attempts=0 WHERE id=? AND lease_token=?", (earliest.astimezone(timezone.utc).isoformat(), job["id"], job["lease_token"]))

    def send(self, job):
        config = load_config(self.store)
        mode = job["payload"].get("mode", "automatic")
        if mode == "automatic":
            local = datetime.now(ZoneInfo("Europe/Berlin"))
            profile = self.store.profile()
            if not profile.contact_start_hour <= local.hour < profile.contact_end_hour:
                self.defer(job)
                return "deferred"
            with self.store.connect() as conn:
                count = conn.execute("SELECT count(*) FROM android_deliveries WHERE intent_at>=?", (datetime.now(timezone.utc).date().isoformat(),)).fetchone()[0]
            if count >= config.daily_sends:
                tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                self.defer(job, tomorrow)
                return "deferred"
        attempt = None
        try:
            with self.phone(config) as phone:
                attempt, message = self.ledger.reserve(job["payload"]["message_id"], mode)
                ad = self.ledger.ad(message["listing_id"])
                phone.locate(ad["card_identity"])
                snapshot = phone.snapshot()
                if snapshot["fingerprint"] != ad["fingerprint"]:
                    self.import_ad(ad["card_identity"], snapshot, config)
                    raise ValueError("The live listing changed since analysis; re-verify its facts and regenerate the message")
                target = phone.conversation(snapshot)
                incoming, outgoing = self.incoming(message["listing_id"], phone, target)
                if message["body"] in outgoing:
                    proof = phone.proof(target, message["body"])
                    self.ledger.delivered(attempt, "Observed an existing exact outgoing app message: " + json.dumps(proof))
                    phone.feed()
                    return "done"
                if message["kind"] == "initial" and (incoming or outgoing):
                    raise ValueError("The app already has conversation history for this listing; review it before another enquiry")
                self.ledger.ready(attempt)
                phone.compose(target, message["body"])
                phone.click_send(target, message["body"], lambda: self.ledger.begin(attempt))
                proof = phone.proof(target, message["body"])
                self.ledger.delivered(attempt, "Exact outgoing app message observed: " + json.dumps(proof))
                phone.feed()
                return "done"
        except Exception as exc:
            if attempt is not None:
                self.ledger.fail(attempt, str(exc) if isinstance(exc, (ValueError, RuntimeError)) else "Phone action failed; inspect its delivery evidence")
            if isinstance(exc, CalibrationRequired):
                self.pause(str(exc))
            elif isinstance(exc, PhoneUnavailable):
                self.store.connector("android", "waiting", str(exc))
                delivery = next((row for row in self.ledger.deliveries() if row["id"] == attempt), None)
                if delivery and delivery["status"] == "sent":
                    return "done"
                if attempt is None or (delivery and not delivery["intent_at"]):
                    self.defer(job, datetime.now(timezone.utc) + timedelta(seconds=config.poll_seconds))
                    return "deferred"
            if attempt is not None and any(row["id"] == attempt and row["status"] == "sent" for row in self.ledger.deliveries()):
                return "done"  # A navigation failure does not erase observed delivery.
            raise
