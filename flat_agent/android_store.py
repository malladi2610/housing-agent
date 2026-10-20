"""Persistent phone leases and send intent. Uncertain clicks are never replayed."""
from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .android_config import AndroidConfig, load_config
from .db import now
from .matching import evaluate
from .models import CLOSED_STAGES, ListingInput, Profile


def body_hash(body):
    return hashlib.sha256(body.encode()).hexdigest()


class DeviceLedger:
    def __init__(self, store):
        self.store = store

    @contextmanager
    def lock(self):
        token = uuid.uuid4().hex
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM device_locks WHERE name='android'").fetchone()
            if row and row["lease_until"] > now():
                raise ValueError("Another phone task is running; wait for it to finish")
            until = (datetime.now(timezone.utc) + timedelta(minutes=6)).isoformat()
            conn.execute("INSERT INTO device_locks VALUES('android',?,?) ON CONFLICT(name) DO UPDATE SET token=excluded.token,lease_until=excluded.lease_until", (token, until))
        try:
            yield lambda: self.check_lock(token)
        finally:
            with self.store.connect() as conn:
                conn.execute("DELETE FROM device_locks WHERE name='android' AND token=?", (token,))

    def check_lock(self, token):
        with self.store.connect() as conn:
            row = conn.execute("SELECT * FROM device_locks WHERE name='android' AND token=? AND lease_until>?", (token, now())).fetchone()
        if not row:
            raise ValueError("Phone task lost its lease; no further action is allowed")
        profile = self.store.profile()
        if profile.paused or profile.search_complete:
            raise ValueError("Search is paused or complete")

    def ad(self, listing_id):
        with self.store.connect() as conn:
            row = conn.execute("SELECT * FROM android_ads WHERE listing_id=?", (listing_id,)).fetchone()
        if not row:
            raise ValueError("This listing has not been captured from the configured Android app")
        result = dict(row)
        result["snapshot"] = json.loads(result["snapshot"])
        return result

    def ads(self):
        with self.store.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM android_ads ORDER BY last_seen,listing_id")]

    def save_ad(self, listing_id, identity, snapshot, config):
        with self.store.connect() as conn:
            conn.execute("INSERT INTO android_ads VALUES(?,?,?,?,?,?,?) ON CONFLICT(listing_id) DO UPDATE SET card_identity=excluded.card_identity,fingerprint=excluded.fingerprint,package=excluded.package,udid=excluded.udid,snapshot=excluded.snapshot,last_seen=excluded.last_seen", (listing_id, identity, snapshot["fingerprint"], config.package, config.udid, json.dumps(snapshot), now()))

    def approve(self, message_id, version, exact_body):
        message = self.store.message(message_id)
        if message["status"] != "draft" or message["version"] != version or message["body"] != exact_body:
            raise ValueError("Save and reload the exact draft before approving a phone send")
        self.store.set_setting(f"android_approval:{message_id}", {"version": version, "body_hash": body_hash(exact_body), "config_revision": self.store.setting("android_revision", 0)})

    def _validate(self, conn, message_id, mode):
        def setting(key, default=None):
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default
        profile = Profile.model_validate(setting("profile"))
        config = AndroidConfig.model_validate(setting("android_config", {}))
        config.require_send()
        if not config.enabled or profile.paused or profile.search_complete:
            raise ValueError("Phone monitoring/search is paused")
        if not profile.first_name.strip() or not profile.introduction.strip():
            raise ValueError("Complete your truthful profile before sending an enquiry")
        pilot = setting("android_pilot", {})
        if pilot.get("fingerprint") != config.fingerprint() or not pilot.get("conversation_verified"):
            raise ValueError("Repeat the phone conversation pilot after changing its configuration")
        row = conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        if not row or row["status"] != "draft":
            raise ValueError("This message is no longer an unsent draft")
        message = dict(row)
        listing = conn.execute("SELECT * FROM listings WHERE id=?", (message["listing_id"],)).fetchone()
        ad = conn.execute("SELECT * FROM android_ads WHERE listing_id=?", (message["listing_id"],)).fetchone()
        if not ad or ad["udid"] != config.udid or ad["package"] != config.package:
            raise ValueError("The listing belongs to a different phone/app; capture it on this device first")
        if listing["stage"] in CLOSED_STAGES or listing["stage"] in {"viewing_proposed", "viewing_confirmed", "viewed"}:
            raise ValueError("The listing is closed or a viewing is already in progress")
        if evaluate(ListingInput.model_validate_json(listing["payload"]), profile).status != "match":
            raise ValueError("Phone sends require a verified match, including total costs, dates and route distance")
        if message["profile_revision"] != profile.revision or message["listing_version"] != listing["version"]:
            raise ValueError("Draft is stale; regenerate it before sending")
        inbound = conn.execute("SELECT id FROM messages WHERE listing_id=? AND direction='inbound' ORDER BY id DESC LIMIT 1", (message["listing_id"],)).fetchone()
        if inbound and inbound[0] != message["in_reply_to"]:
            raise ValueError("A newer reply arrived; review the conversation")
        if message["kind"] == "initial" and conn.execute("SELECT 1 FROM messages WHERE listing_id=? AND (status='sent' OR direction='inbound')", (message["listing_id"],)).fetchone():
            raise ValueError("This conversation already has messages; review it before replying")
        revision = setting("android_revision", 0)
        if mode == "automatic":
            review = setting(f"draft_review:{message_id}", {})
            if config.mode != "automatic" or message["kind"] not in {"initial", "followup"}:
                raise ValueError("Only initial enquiries and enabled follow-ups can be sent automatically")
            if message["kind"] == "followup" and not config.auto_followups:
                raise ValueError("Automatic follow-up sending is disabled")
            if not review.get("grounded") or review.get("needs_user_input") or review.get("body_hash") != body_hash(message["body"]):
                raise ValueError("The draft needs review or was edited; approve this exact message manually")
            local = datetime.now(ZoneInfo("Europe/Berlin"))
            if not profile.contact_start_hour <= local.hour < profile.contact_end_hour:
                raise ValueError("Outside contact hours; automatic send must be deferred")
        elif mode == "approved":
            approval = setting(f"android_approval:{message_id}", {})
            if approval != {"version": message["version"], "body_hash": body_hash(message["body"]), "config_revision": revision}:
                raise ValueError("This exact draft/configuration has not been approved")
        else:
            raise ValueError("Unknown phone-send authorization")
        return message, profile, config, revision

    def reserve(self, message_id, mode):
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            message, profile, config, revision = self._validate(conn, message_id, mode)
            uncertain = conn.execute("SELECT 1 FROM android_deliveries WHERE status IN ('sending','uncertain') LIMIT 1").fetchone()
            if uncertain:
                raise ValueError("An earlier phone delivery is uncertain. Reconcile it before sending again.")
            existing = conn.execute("SELECT * FROM android_deliveries WHERE message_id=?", (message_id,)).fetchone()
            if existing and existing["status"] not in {"cancelled"}:
                raise ValueError("A phone attempt already exists for this message; it will not be replayed")
            today = datetime.now(timezone.utc).date().isoformat()
            count = conn.execute("SELECT count(*) FROM android_deliveries WHERE intent_at>=?", (today,)).fetchone()[0]
            if count >= config.daily_sends:
                raise ValueError("Daily phone-send limit reached")
            values = (message["listing_id"], message["version"], message["listing_version"], profile.revision, revision, body_hash(message["body"]), mode, now(), message_id)
            if existing:
                conn.execute("UPDATE android_deliveries SET listing_id=?,message_version=?,listing_version=?,profile_revision=?,config_revision=?,body_hash=?,mode=?,created_at=?,status='preparing',intent_at=NULL,completed_at=NULL,evidence='' WHERE message_id=?", values)
                identifier = existing["id"]
            else:
                identifier = conn.execute("INSERT INTO android_deliveries(listing_id,message_version,listing_version,profile_revision,config_revision,body_hash,mode,created_at,message_id,status) VALUES(?,?,?,?,?,?,?,?,?,'preparing')", values).lastrowid
            return identifier, message

    def begin(self, identifier):
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            attempt = conn.execute("SELECT * FROM android_deliveries WHERE id=?", (identifier,)).fetchone()
            if not attempt or attempt["status"] != "preparing":
                raise ValueError("Phone delivery is not ready for a new send")
            message, profile, config, revision = self._validate(conn, attempt["message_id"], attempt["mode"])
            if (attempt["body_hash"], attempt["message_version"], attempt["config_revision"]) != (body_hash(message["body"]), message["version"], revision):
                raise ValueError("The message or phone settings changed during preparation")
            today = datetime.now(timezone.utc).date().isoformat()
            if conn.execute("SELECT count(*) FROM android_deliveries WHERE intent_at>=?", (today,)).fetchone()[0] >= config.daily_sends:
                raise ValueError("Daily phone-send limit reached")
            conn.execute("UPDATE android_deliveries SET status='sending',intent_at=? WHERE id=?", (now(), identifier))
            self.store._event(conn, message["listing_id"], "phone_send_intent", f"Delivery attempt {identifier}; exact body/version reserved before app click")

    def ready(self, identifier):
        with self.store.connect() as conn:
            row = conn.execute("SELECT * FROM android_deliveries WHERE id=?", (identifier,)).fetchone()
            if not row or row["status"] != "preparing":
                raise ValueError("The phone delivery is no longer being prepared")
            message, _, _, revision = self._validate(conn, row["message_id"], row["mode"])
            if (row["body_hash"], row["message_version"], row["config_revision"]) != (body_hash(message["body"]), message["version"], revision):
                raise ValueError("The draft/configuration changed during phone preparation")

    def fail(self, identifier, detail):
        with self.store.connect() as conn:
            row = conn.execute("SELECT * FROM android_deliveries WHERE id=?", (identifier,)).fetchone()
            if row and row["status"] in {"preparing", "sending"}:
                status = "uncertain" if row["intent_at"] else "cancelled"
                conn.execute("UPDATE android_deliveries SET status=?,evidence=?,completed_at=? WHERE id=?", (status, detail[:1000], now(), identifier))
                self.store._event(conn, row["listing_id"], "phone_delivery_" + status, detail[:600])

    def delivered(self, identifier, evidence):
        if not evidence:
            raise ValueError("Delivery requires explicit evidence")
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            attempt = conn.execute("SELECT * FROM android_deliveries WHERE id=?", (identifier,)).fetchone()
            if not attempt or attempt["status"] not in {"preparing", "sending", "uncertain"}:
                raise ValueError("This delivery cannot be confirmed again")
            message = conn.execute("SELECT * FROM messages WHERE id=?", (attempt["message_id"],)).fetchone()
            if body_hash(message["body"]) != attempt["body_hash"]:
                raise ValueError("Saved message differs from the observed delivery; reconcile manually")
            conn.execute("UPDATE android_deliveries SET status='sent',completed_at=?,evidence=? WHERE id=?", (now(), evidence, identifier))
            conn.execute("UPDATE messages SET status='sent',sent_at=?,delivery_evidence=? WHERE id=?", (now(), evidence, message["id"]))
            # Record the fact of delivery even if a reply/profile change raced with proof.
            listing = conn.execute("SELECT * FROM listings WHERE id=?", (message["listing_id"],)).fetchone()
            inbound = conn.execute("SELECT 1 FROM messages WHERE listing_id=? AND direction='inbound' AND id>?", (message["listing_id"], message["id"])).fetchone()
            if not inbound and listing["stage"] not in CLOSED_STAGES and listing["stage"] not in {"viewing_proposed", "viewing_confirmed", "viewed"}:
                conn.execute("UPDATE listings SET stage='message_sent',updated_at=? WHERE id=?", (now(), message["listing_id"]))
                if message["kind"] == "initial":
                    due = (datetime.now(timezone.utc) + timedelta(days=self.store.profile().followup_days)).isoformat()
                    self.store._enqueue(conn, "followup", message["listing_id"], {"sent_message_id": message["id"]}, f"followup:{message['id']}", due)
            self.store._event(conn, message["listing_id"], "message_sent", evidence)

    def deliveries(self):
        with self.store.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM android_deliveries ORDER BY id DESC LIMIT 50")]

    def reconcile(self, identifier, outcome, evidence):
        if len(evidence.strip()) < 10 or len(evidence) > 2000:
            raise ValueError("Describe the delivery evidence you checked on the phone")
        if outcome == "sent":
            self.delivered(identifier, "Owner reconciled phone delivery: " + evidence.strip())
        elif outcome == "not_sent":
            with self.store.connect() as conn:
                cursor = conn.execute("UPDATE android_deliveries SET status='cancelled',evidence=?,completed_at=? WHERE id=? AND status IN ('sending','uncertain','preparing')", ("Owner verified no delivery: " + evidence.strip(), now(), identifier))
                if not cursor.rowcount:
                    raise ValueError("This delivery does not need reconciliation")
                self.store._event(conn, None, "phone_delivery_reconciled", f"Attempt {identifier}: owner verified not sent; retry requires a deliberate job retry")
        else:
            raise ValueError("Choose whether the exact message was sent")
