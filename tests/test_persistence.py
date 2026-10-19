from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from flat_agent.db import Store
from flat_agent.models import Stage, ViewingInput


def make_draft(store, listing_data):
    identifier,_ = store.add_listing(listing_data)
    listing = store.listing(identifier)
    msg = store.add_draft(identifier,"Hello, I would like to view the room.","test",store.profile().revision,listing["version"])
    return identifier,msg


def test_restart_deduplication_and_consistent_backup(store,listing_data,tmp_path):
    identifier,created = store.add_listing(listing_data)
    assert created
    second,created = store.add_listing(listing_data)
    assert second==identifier and not created
    job = store.enqueue("draft",identifier,dedupe_key="same-job")
    assert store.enqueue("draft",identifier,dedupe_key="same-job")==job
    restarted = Store(store.path)
    assert restarted.listing(identifier)["data"]["title"]==listing_data.title
    assert len(restarted.jobs())==1
    backup = tmp_path/"backup.sqlite3"
    store.backup(backup)
    assert Store(backup).listing(identifier)["data"]==store.listing(identifier)["data"]


def test_atomic_job_claims_and_lease_recovery(store,listing_data):
    identifier,_ = store.add_listing(listing_data)
    for _ in range(8):
        store.enqueue("analyze",identifier)
    with ThreadPoolExecutor(max_workers=4) as pool:
        claimed = list(pool.map(lambda _:Store(store.path).claim_job(),range(8)))
    assert len({job["id"] for job in claimed})==8
    with store.connect() as conn:
        conn.execute("UPDATE jobs SET lease_until='2000-01-01T00:00:00+00:00' WHERE id=?", (claimed[0]["id"],))
    recovered = store.claim_job()
    assert recovered["id"]==claimed[0]["id"]
    assert recovered["lease_token"]!=claimed[0]["lease_token"]
    store.finish_job(claimed[0])
    assert next(j for j in store.jobs() if j["id"]==recovered["id"])["status"]=="running"


def test_send_requires_exact_saved_text_and_stale_drafts_fail(store,listing_data,ready_profile):
    identifier,message_id = make_draft(store,listing_data)
    message = store.message(message_id)
    with pytest.raises(ValueError):
        store.record_sent(message_id,message["version"],"Different text",True)
    with pytest.raises(ValueError):
        store.record_sent(message_id,message["version"],message["body"],False)
    profile = store.profile()
    profile.introduction += " Updated."
    store.save_profile(profile)
    with pytest.raises(ValueError,match="stale"):
        store.record_sent(message_id,message["version"],message["body"],True)
    assert store.message(message_id)["status"]=="draft"


def test_reply_cancels_followup_and_prevents_duplicate_receipts(store,listing_data,ready_profile):
    identifier,message_id = make_draft(store,listing_data)
    message = store.message(message_id)
    store.record_sent(message_id,1,message["body"],True)
    assert len([j for j in store.jobs() if j["kind"]=="followup"])==1
    with pytest.raises(ValueError):
        store.record_sent(message_id,1,message["body"],True)
    first_reply = store.add_reply(identifier,"Would you like to visit?",external_id="reply-1")
    assert store.add_reply(identifier,"Duplicate",external_id="reply-1")==first_reply
    assert store.jobs()[0]["status"]=="cancelled"
    assert store.listing(identifier)["stage"]=="response_received"


def test_confirmed_viewings_require_evidence_and_avoid_conflicts(store,listing_data):
    identifier,_ = store.add_listing(listing_data)
    starts = datetime.now(timezone.utc)+timedelta(days=7)
    with pytest.raises(ValueError):
        ViewingInput(starts_at=starts,location="MTZ",confirmed=True)
    store.add_viewing(identifier,ViewingInput(starts_at=starts,location="Listing address",confirmed=False))
    assert store.listing(identifier)["stage"]=="viewing_proposed"
    store.add_viewing(identifier,ViewingInput(starts_at=starts,location="Listing address",confirmed=True,confirmation_evidence="Advertiser accepted the exact date/time"))
    assert store.listing(identifier)["stage"]=="viewing_confirmed"
    with pytest.raises(ValueError,match="conflicts"):
        store.add_viewing(identifier,ViewingInput(starts_at=starts+timedelta(minutes=40),location="Other flat",confirmed=True,confirmation_evidence="Accepted"))


def test_pause_prevents_job_claims_and_quota_is_atomic(store):
    profile = store.profile()
    profile.paused=True
    store.save_profile(profile)
    store.enqueue("sync_imap")
    assert store.claim_job() is None
    profile.paused=False
    profile.daily_model_calls=1
    store.save_profile(profile)
    store.reserve_model_call("draft_message","test")
    with pytest.raises(ValueError,match="Daily"):
        store.reserve_model_call("draft_message","test")


def test_cancelled_viewing_releases_the_calendar_slot(store,listing_data):
    identifier,_=store.add_listing(listing_data)
    starts=datetime.now(timezone.utc)+timedelta(days=5)
    data=ViewingInput(starts_at=starts,location="Test address",confirmed=True,confirmation_evidence="Confirmed by advertiser")
    viewing_id=store.add_viewing(identifier,data)
    store.cancel_viewing(viewing_id)
    assert store.viewings(identifier)[0]["status"]=="cancelled"
    assert store.listing(identifier)["stage"]=="matched"
    assert store.add_viewing(identifier,data)!=viewing_id
