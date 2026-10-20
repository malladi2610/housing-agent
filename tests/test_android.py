import copy
from datetime import datetime, timedelta, timezone

import pytest

from flat_agent.android import AndroidPipeline
from flat_agent.android_config import AndroidConfig, SCAN_ROLES, SEND_ROLES, Selector, load_config, save_config
from flat_agent.android_phone import CalibrationRequired, PhoneUnavailable, content_hash
from flat_agent.android_store import DeviceLedger, body_hash
from flat_agent.models import ListingInput
from flat_agent.workflows import Workflows


class FakePhone:
    """Stateful fake hardware, including a click that succeeds before transport loss."""
    def __init__(self, snapshot, identity="immutable-ad-123"):
        self.ad = snapshot
        self.identity = identity
        self.outgoing = []
        self.inbound = []
        self.clicks = 0
        self.draft = ""
        self.drop_after_click = False
        self.no_proof = False
        self.before_send = lambda: None
        self.guard = lambda: None

    def factory(self, settings, store, config, guard):
        self.guard = guard
        return self

    def connect(self): self.guard()
    def close(self): pass
    def feed(self): self.guard()
    def refresh_feed(self): self.guard()
    def cards(self): return {self.identity: "card"}
    def open_card(self, identity): assert identity == self.identity
    def locate(self, identity): assert identity == self.identity
    def snapshot(self): return copy.deepcopy(self.ad)
    def conversation(self, snapshot): return snapshot["title"]
    def messages(self, expected): return list(self.inbound), list(self.outgoing)
    def one(self, role): return role
    def scroll(self, direction): return False

    def compose(self, expected, body):
        self.guard()
        if self.draft:
            raise CalibrationRequired("Existing draft was not overwritten")
        self.draft = body

    def click_send(self, expected, body, before_click):
        self.before_send()
        before_click()
        self.clicks += 1
        self.outgoing.append(body)
        self.draft = ""
        if self.drop_after_click:
            raise PhoneUnavailable("USB disconnected after the click")

    def proof(self, expected, body):
        if self.no_proof:
            raise CalibrationRequired("No exact outgoing proof")
        assert body in self.outgoing
        return {"thread": expected, "body_sha256": body_hash(body), "outgoing_element": "observed-bubble"}


@pytest.fixture
def android_config():
    return AndroidConfig(udid="test-usb-phone", package="example.rentals", activity=".MainActivity", refresh="none", mode="automatic", selectors={role: Selector(value=f"example.rentals:id/{role}") for role in SCAN_ROLES | SEND_ROLES | {"incoming"}})


@pytest.fixture
def phone_setup(settings, store, listing_data, ready_profile, fake_router, android_config):
    snapshot = {"title": listing_data.title, "body": listing_data.description, "contact": "Mira"}
    snapshot["fingerprint"] = content_hash(snapshot["title"], snapshot["body"], snapshot["contact"])
    phone = FakePhone(snapshot)
    pipeline = AndroidPipeline(settings, store, phone.factory)
    identifier = pipeline.import_ad(phone.identity, snapshot, android_config)
    current = store.listing(identifier)
    verified = listing_data.model_copy(update={"source": "flatsforfriendz", "external_id": current["data"]["external_id"], "url": None, "contact_name": "Mira"})
    store.update_listing(identifier, verified, current["version"])
    store.set_setting("android_pilot", {"fingerprint": android_config.fingerprint(), "conversation_verified": True})
    save_config(store, android_config.model_copy(update={"enabled": True}), acknowledged=True)
    workflows = Workflows(store, fake_router, settings.data_dir / "checkpoints.sqlite3")
    message_id = workflows.draft(identifier)
    return pipeline, phone, identifier, message_id


def send_job(store, identifier, message_id, mode="automatic"):
    job_id = store.enqueue("android_send", identifier, {"message_id": message_id, "mode": mode})
    job = store.claim_job()
    assert job["id"] == job_id
    return job


def test_phone_delivery_has_exact_proof_and_one_followup(store, phone_setup):
    pipeline, phone, identifier, message_id = phone_setup
    job = send_job(store, identifier, message_id)
    assert pipeline.send(job) == "done"
    assert phone.clicks == 1
    assert store.message(message_id)["status"] == "sent"
    assert "Exact outgoing" in store.message(message_id)["delivery_evidence"]
    assert len([j for j in store.jobs() if j["kind"] == "followup"]) == 1
    assert pipeline.ledger.deliveries()[0]["status"] == "sent"
    with pytest.raises(ValueError):
        pipeline.send(job)
    assert phone.clicks == 1


def test_restart_never_replays_a_click_with_lost_receipt(settings, store, phone_setup):
    pipeline, phone, identifier, message_id = phone_setup
    phone.drop_after_click = True
    job = send_job(store, identifier, message_id)
    with pytest.raises(PhoneUnavailable):
        pipeline.send(job)
    assert phone.clicks == 1
    assert store.message(message_id)["status"] == "draft"
    assert pipeline.ledger.deliveries()[0]["status"] == "uncertain"
    replacement = AndroidPipeline(settings, store, phone.factory)
    phone.drop_after_click = False
    with pytest.raises(ValueError, match="uncertain"):
        replacement.send(job)
    assert phone.clicks == 1
    assert not [j for j in store.jobs() if j["kind"] == "followup"]
    replacement.ledger.reconcile(replacement.ledger.deliveries()[0]["id"], "sent", "I checked the exact outgoing message on the phone.")
    assert store.message(message_id)["status"] == "sent"


def test_missing_outgoing_proof_disarms_and_does_not_claim_sent(store, phone_setup):
    pipeline, phone, identifier, message_id = phone_setup
    phone.no_proof = True
    with pytest.raises(CalibrationRequired):
        pipeline.send(send_job(store, identifier, message_id))
    assert not load_config(store).enabled
    assert phone.clicks == 1
    assert store.message(message_id)["status"] == "draft"
    assert pipeline.ledger.deliveries()[0]["status"] == "uncertain"


def test_changed_live_listing_invalidates_prior_verified_facts(store, phone_setup):
    pipeline, phone, identifier, message_id = phone_setup
    phone.ad["body"] += " Rent has changed to 950 EUR."
    phone.ad["fingerprint"] = content_hash(phone.ad["title"], phone.ad["body"], phone.ad["contact"])
    with pytest.raises(ValueError, match="changed"):
        pipeline.send(send_job(store, identifier, message_id))
    assert phone.clicks == 0
    assert store.listing(identifier)["match"]["status"] == "needs_review"
    assert not store.listing(identifier)["data"]["all_costs_confirmed"]
    assert pipeline.ledger.deliveries()[0]["status"] == "cancelled"


def test_new_incoming_reply_cancels_stale_enquiry_before_send(store, phone_setup):
    pipeline, phone, identifier, message_id = phone_setup
    phone.inbound = ["The room is no longer available."]
    with pytest.raises(ValueError):
        pipeline.send(send_job(store, identifier, message_id))
    assert phone.clicks == 0
    assert store.message(message_id)["status"] == "superseded"
    assert any(m["direction"] == "inbound" for m in store.messages(identifier))


def test_pause_just_before_click_prevents_side_effect(store, phone_setup):
    pipeline, phone, identifier, message_id = phone_setup
    def pause():
        profile = store.profile()
        profile.paused = True
        store.save_profile(profile)
    phone.before_send = pause
    with pytest.raises(ValueError, match="paused"):
        pipeline.send(send_job(store, identifier, message_id))
    assert phone.clicks == 0
    assert pipeline.ledger.deliveries()[0]["intent_at"] is None


def test_edited_text_requires_new_exact_approval(store, phone_setup):
    pipeline, phone, identifier, message_id = phone_setup
    store.edit_draft(message_id, "Hallo Mira, ich würde das ruhige Zimmer gerne besichtigen. Viele Grüße, Alex", 1)
    with pytest.raises(ValueError, match="edited"):
        pipeline.send(send_job(store, identifier, message_id))
    message = store.message(message_id)
    pipeline.ledger.approve(message_id, message["version"], message["body"])
    assert pipeline.send({"payload": {"message_id": message_id, "mode": "approved"}}) == "done"
    assert phone.outgoing == [message["body"]]


def test_model_requests_for_missing_input_never_send_automatically(store, phone_setup):
    pipeline, phone, identifier, message_id = phone_setup
    review = store.setting(f"draft_review:{message_id}")
    review["needs_user_input"] = ["Provide your availability"]
    store.set_setting(f"draft_review:{message_id}", review)
    with pytest.raises(ValueError, match="needs review"):
        pipeline.send(send_job(store, identifier, message_id))
    assert phone.clicks == 0


def test_phone_lock_is_exclusive_and_old_owner_cannot_keep_acting(store):
    first, second = DeviceLedger(store), DeviceLedger(store)
    with first.lock() as guard:
        with pytest.raises(ValueError, match="Another phone"):
            with second.lock():
                pass
        with store.connect() as conn:
            conn.execute("UPDATE device_locks SET lease_until=?", ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),))
        with second.lock() as new_guard:
            with pytest.raises(ValueError, match="lost its lease"):
                guard()
            new_guard()


def test_pilot_is_read_only_and_bound_to_device_selectors(settings, store, android_config, listing_data):
    snapshot = {"title": listing_data.title, "body": listing_data.description, "contact": "Mira"}
    snapshot["fingerprint"] = content_hash(*snapshot.values())
    phone = FakePhone(snapshot)
    save_config(store, android_config)
    pipeline = AndroidPipeline(settings, store, phone.factory)
    identifier = pipeline.pilot()
    assert phone.clicks == 0 and not phone.draft
    assert store.setting("android_pilot")["conversation_verified"]
    assert store.setting("android_pilot")["listing_id"] == identifier
    save_config(store, android_config.model_copy(update={"enabled": True}), acknowledged=True)
    changed = android_config.model_copy(update={"udid": "a-different-phone", "enabled": True})
    with pytest.raises(ValueError, match="pilot"):
        save_config(store, changed, acknowledged=True)


def test_polls_deduplicate_source_and_repeated_replies(settings, store, phone_setup):
    pipeline, phone, identifier, message_id = phone_setup
    pipeline.send(send_job(store, identifier, message_id))
    phone.inbound = ["Can you view on Saturday?"]
    pipeline.poll()
    pipeline.poll()
    assert len(store.listings()) == 1
    assert len([m for m in store.messages(identifier) if m["direction"] == "inbound"]) == 1
    assert next(j for j in store.jobs() if j["kind"] == "followup")["status"] == "cancelled"


def test_stale_draft_and_unconfirmed_route_cannot_send(store, phone_setup):
    pipeline, phone, identifier, message_id = phone_setup
    listing = store.listing(identifier)
    data = ListingInput.model_validate(listing["data"]).model_copy(update={"distance_basis": "straight_line"})
    store.update_listing(identifier, data, listing["version"])
    with pytest.raises(ValueError, match="verified match"):
        pipeline.send(send_job(store, identifier, message_id))
    assert phone.clicks == 0


def test_outside_contact_hours_defers_without_touching_phone(store, phone_setup, monkeypatch):
    import flat_agent.android as module
    pipeline, phone, identifier, message_id = phone_setup
    profile = store.profile()
    profile.contact_start_hour, profile.contact_end_hour = 9, 20
    store.save_profile(profile)
    class Night(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 20, 1, tzinfo=timezone.utc).astimezone(tz) if tz else datetime(2026, 10, 20, 1)
    monkeypatch.setattr(module, "datetime", Night)
    job = send_job(store, identifier, message_id)
    assert pipeline.send(job) == "deferred"
    assert phone.clicks == 0
    queued = next(j for j in store.jobs() if j["id"] == job["id"])
    assert queued["status"] == "queued" and queued["lease_token"] is None
    assert datetime.fromisoformat(queued["due_at"]).hour == 7  # 09:00 Europe/Berlin before DST ends.


def test_uncertain_delivery_cannot_be_edited_or_manually_overwritten(store, phone_setup):
    pipeline, phone, identifier, message_id = phone_setup
    phone.drop_after_click = True
    with pytest.raises(PhoneUnavailable):
        pipeline.send(send_job(store, identifier, message_id))
    message = store.message(message_id)
    with pytest.raises(ValueError, match="reconcile"):
        store.edit_draft(message_id, "A different message", message["version"])
    with pytest.raises(ValueError, match="Reconcile"):
        store.record_sent(message_id, message["version"], message["body"], True)
    assert store.message(message_id)["body"] == message["body"]


def test_send_ceiling_is_enforced_at_the_final_click_boundary(store, phone_setup):
    pipeline, phone, identifier, message_id = phone_setup
    attempt, _ = pipeline.ledger.reserve(message_id, "automatic")
    # Another action can consume the quota between preparing and clicking.
    config = load_config(store)
    config.daily_sends = 1
    store.set_setting("android_config", config.model_dump())
    with store.connect() as conn:
        conn.execute("UPDATE android_deliveries SET intent_at=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), attempt))
    with pytest.raises(ValueError, match="limit"):
        pipeline.ledger.begin(attempt)
    assert phone.clicks == 0


def test_full_worker_pipeline_waits_for_route_facts_then_sends_once(settings, store, listing_data, ready_profile, fake_router, android_config):
    from flat_agent.worker import Worker
    snapshot = {"title": listing_data.title, "body": listing_data.description, "contact": "Mira"}
    snapshot["fingerprint"] = content_hash(snapshot["title"], snapshot["body"], snapshot["contact"])
    phone = FakePhone(snapshot)
    pipeline = AndroidPipeline(settings, store, phone.factory)
    settings.save_secret("OLLAMA_API_KEY", "test-not-a-real-key")
    store.set_setting("android_pilot", {"fingerprint": android_config.fingerprint(), "conversation_verified": True})
    save_config(store, android_config.model_copy(update={"enabled": True}), acknowledged=True)
    pipeline.poll()
    worker = Worker(settings, store, fake_router)
    worker.android = pipeline
    worker.tick()  # Analyze; no route has been verified.
    identifier = store.listings()[0]["id"]
    assert store.listing(identifier)["match"]["status"] == "needs_review"
    assert phone.clicks == 0 and not store.messages(identifier)
    current = store.listing(identifier)
    verified = ListingInput.model_validate(current["data"]).model_copy(update={"distance_km": 3.5, "distance_basis": "route", "commute_evidence": "Owner verified the cycling route to MTZ.", "cycling_minutes": 14, "transit_minutes": 22})
    store.update_listing(identifier, verified, current["version"])
    pipeline.queue_ready()
    for _ in range(8):
        worker.tick()
        if phone.clicks:
            break
    assert phone.clicks == 1
    assert [m["status"] for m in store.messages(identifier)] == ["sent"]
    assert fake_router.calls == ["extract_and_classify", "draft_message"]


def test_usb_loss_before_a_click_defers_and_resumes_the_same_job(store, phone_setup, monkeypatch):
    pipeline, phone, identifier, message_id = phone_setup
    original = phone.locate
    def offline(identity):
        raise PhoneUnavailable("USB is temporarily disconnected")
    monkeypatch.setattr(phone, "locate", offline)
    job = send_job(store, identifier, message_id)
    assert pipeline.send(job) == "deferred"
    attempt = pipeline.ledger.deliveries()[0]
    assert attempt["status"] == "cancelled" and attempt["intent_at"] is None
    assert phone.clicks == 0
    monkeypatch.setattr(phone, "locate", original)
    with store.connect() as conn:
        conn.execute("UPDATE jobs SET due_at=? WHERE id=?", ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), job["id"]))
    retry = store.claim_job()
    assert retry["id"] == job["id"]
    assert pipeline.send(retry) == "done"
    assert phone.clicks == 1 and store.message(message_id)["status"] == "sent"


def test_existing_exact_outgoing_message_is_observed_without_clicking_again(store, phone_setup):
    pipeline, phone, identifier, message_id = phone_setup
    phone.outgoing = [store.message(message_id)["body"]]
    assert pipeline.send(send_job(store, identifier, message_id)) == "done"
    assert phone.clicks == 0
    assert "existing exact outgoing" in store.message(message_id)["delivery_evidence"]


def test_an_existing_phone_draft_is_not_overwritten(store, phone_setup):
    pipeline, phone, identifier, message_id = phone_setup
    phone.draft = "My own unfinished message"
    with pytest.raises(CalibrationRequired, match="not overwritten"):
        pipeline.send(send_job(store, identifier, message_id))
    assert phone.draft == "My own unfinished message" and phone.clicks == 0


def test_poll_scans_later_pages_and_rotates_past_already_seen_ads(settings, store, android_config):
    class PagedPhone(FakePhone):
        def __init__(self):
            super().__init__({})
            self.page = 0
            self.opened = ""
        def refresh_feed(self): self.page = 0
        def cards(self): return {f"stable-{i}": "element" for i in ([1, 2] if self.page == 0 else [3])}
        def scroll(self, direction): self.page += 1; return True
        def locate(self, identity): self.opened = identity
        def snapshot(self):
            title = "Synthetic " + self.opened
            return {"title": title, "body": "A room with unconfirmed facts", "contact": "", "fingerprint": content_hash(title, "A room with unconfirmed facts", "")}
    config = android_config.model_copy(update={"enabled": True, "mode": "review", "max_cards": 1})
    save_config(store, config)
    phone = PagedPhone()
    pipeline = AndroidPipeline(settings, store, phone.factory)
    for _ in range(3):
        pipeline.poll()
    assert {a["card_identity"] for a in pipeline.ledger.ads()} == {"stable-1", "stable-2", "stable-3"}
