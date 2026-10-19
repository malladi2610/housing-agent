from pathlib import Path

import pytest

from flat_agent.db import Store
from flat_agent.models import ListingInput
from flat_agent.settings import Settings


@pytest.fixture
def settings(tmp_path):
    value = Settings(data_dir=tmp_path, OLLAMA_API_KEY="")
    value.prepare()
    return value


@pytest.fixture
def store(settings):
    return Store(settings.database_path)


@pytest.fixture
def listing_data():
    return ListingInput(title="Test WG near MTZ",source="wg-gesucht",url="https://www.wg-gesucht.de/wg-zimmer-in-Muenchen.12345678.html",home_type="shared",all_in_rent=820,all_costs_confirmed=True,available_from="2026-11-15",move_in_confirmed=True,distance_km=3.5,distance_basis="route",cycling_minutes=14,transit_minutes=22,description="All costs included: 820 EUR. Available from 2026-11-15. A quiet room with a shared kitchen.")


class FakeRouter:
    def __init__(self, callback=None):
        self.calls = []
        self.callback = callback

    def ask(self,role,schema,task,data,**kwargs):
        self.calls.append(role)
        if self.callback:
            self.callback()
        values = {
            "extract_and_classify": {"home_type":"shared","all_in_rent":820,"all_costs_confirmed":True,"cost_evidence":"All costs included: 820 EUR.","available_from":"2026-11-15","move_in_confirmed":True,"availability_evidence":"Available from 2026-11-15."},
            "draft_message": {"text":"Hallo, das ruhige Zimmer mit der gemeinsamen Küche passt gut zu mir. Ich würde es gern besichtigen. Viele Grüße, Alex","used_profile_fields":["first_name","introduction"],"listing_detail":"shared kitchen","needs_user_input":[]},
            "interpret_complex_reply": {"intent":"viewing_invitation","summary":"A viewing time was proposed, not confirmed.","suggested_reply":"","needs_user_input":["Confirm the exact date"]},
            "interpret_screen": {"summary":"A rental listing screen","target_description":"Conversation button","proposed_action":"stop","reason":"Recipient needs verification"},
        }
        return schema.model_validate(values[role]),"fake-test-model"


@pytest.fixture
def fake_router():
    return FakeRouter()


@pytest.fixture
def ready_profile(store):
    profile = store.profile()
    profile.first_name = "Alex"
    profile.introduction = "I work near MTZ and am looking for a quiet room."
    profile.contact_start_hour = 0
    profile.contact_end_hour = 24
    store.save_profile(profile)
    return store.profile()
