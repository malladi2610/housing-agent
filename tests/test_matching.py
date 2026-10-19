from flat_agent.matching import evaluate
from flat_agent.models import ListingInput, Profile


def test_all_in_cap_and_unknown_extras(listing_data):
    profile = Profile()
    assert evaluate(listing_data,profile).status == "match"
    too_expensive = listing_data.model_copy(update={"all_in_rent":851})
    assert evaluate(too_expensive,profile).status == "excluded"
    unconfirmed = listing_data.model_copy(update={"all_costs_confirmed":False})
    assert evaluate(unconfirmed,profile).status == "needs_review"
    components = listing_data.model_copy(update={"all_in_rent":None,"warm_rent":780,"electricity":40,"internet":25,"other_monthly":0})
    assert evaluate(components,profile).monthly_total == 845
    assert evaluate(components,profile).status == "match"
    assert evaluate(components.model_copy(update={"internet":None}),profile).status == "needs_review"


def test_dates_are_inclusive_and_early_availability_needs_confirmation(listing_data):
    profile = Profile()
    assert evaluate(ListingInput.model_validate({**listing_data.model_dump(mode="json"),"available_from":"2027-01-01"}),profile).status == "match"
    assert evaluate(ListingInput.model_validate({**listing_data.model_dump(mode="json"),"available_from":"2027-01-02"}),profile).status == "excluded"
    assert evaluate(listing_data.model_copy(update={"move_in_confirmed":False}),profile).status == "needs_review"


def test_distance_is_not_a_commute_time_or_a_radius_guarantee(listing_data):
    assert evaluate(listing_data.model_copy(update={"distance_basis":"straight_line"}),Profile()).status == "needs_review"
    assert evaluate(listing_data.model_copy(update={"distance_km":6.1}),Profile()).status == "excluded"
    assert evaluate(listing_data.model_copy(update={"distance_km":6}),Profile()).status == "match"
