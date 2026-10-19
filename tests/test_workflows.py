import pytest

from flat_agent.db import Store
from flat_agent.models import Stage
from flat_agent.worker import Worker
from flat_agent.workflows import Workflows
from conftest import FakeRouter


def test_langgraph_analysis_persists_and_does_not_repeat_completed_job(store,listing_data,fake_router,settings):
    identifier,_=store.add_listing(listing_data)
    workflow=Workflows(store,fake_router,settings.data_dir/"checkpoints.sqlite3")
    result=workflow.analyze(identifier,42)
    assert result["result"]=="Analysis complete"
    assert store.listing(identifier)["match"]["status"]=="match"
    workflow.analyze(identifier,42)
    assert fake_router.calls==["extract_and_classify"]
    assert store.listing(identifier)["version"]==2


def test_no_profile_means_no_invented_introduction(store,listing_data,fake_router,settings):
    identifier,_=store.add_listing(listing_data)
    workflow=Workflows(store,fake_router,settings.data_dir/"checkpoints.sqlite3")
    with pytest.raises(ValueError,match="introduction"):
        workflow.draft(identifier)
    assert not fake_router.calls


def test_extraction_quote_does_not_validate_a_different_number_or_date(store,listing_data,settings):
    from flat_agent.models import Extraction
    identifier,_=store.add_listing(listing_data.model_copy(update={"all_in_rent":None,"all_costs_confirmed":False,"available_from":None,"move_in_confirmed":False}))
    class UnsupportedClaim:
        def ask(self,*args,**kwargs):
            return Extraction(home_type="shared",all_in_rent=700,all_costs_confirmed=True,cost_evidence="All costs included: 820 EUR.",available_from="2026-12-01",move_in_confirmed=True,availability_evidence="Available from 2026-11-15."),"test"
    Workflows(store,UnsupportedClaim(),settings.data_dir/"checkpoints.sqlite3").analyze(identifier,1)
    data=store.listing(identifier)["data"]
    assert data["all_in_rent"] is None
    assert not data["all_costs_confirmed"]
    assert data["available_from"] is None
    assert not data["move_in_confirmed"]


def test_reply_arriving_while_followup_is_generated_prevents_draft(store,listing_data,ready_profile,settings):
    identifier,_=store.add_listing(listing_data)
    listing=store.listing(identifier)
    draft=store.add_draft(identifier,"Initial enquiry","test",store.profile().revision,listing["version"])
    store.record_sent(draft,1,"Initial enquiry",True)
    fake=FakeRouter(callback=lambda:store.add_reply(identifier,"The room is no longer available"))
    workflow=Workflows(store,fake,settings.data_dir/"checkpoints.sqlite3")
    with pytest.raises(ValueError,match="no longer due"):
        workflow.draft(identifier,kind="followup")
    assert not [m for m in store.messages(identifier) if m["kind"]=="followup"]


def test_worker_picks_persisted_job_and_keeps_failures_visible(store,listing_data,ready_profile,settings,fake_router):
    identifier,_=store.add_listing(listing_data)
    store.enqueue("draft",identifier)
    worker=Worker(settings,store=Store(store.path),router=fake_router)
    assert worker.tick()
    assert store.jobs()[0]["status"]=="done"
    assert store.messages(identifier)[0]["status"]=="draft"
    store.set_stage(identifier,Stage.CLOSED)
    store.enqueue("analyze",identifier)
    worker.tick()
    assert store.jobs()[0]["status"]=="cancelled"
