"""The hard rules (distance, price, dates), ranking, and the SQLite store."""
from datetime import date, timedelta

from homeward.core import km_between, km_to_work, listing_from_row, parse_euro, parse_period, check, score

from conftest import MTZ, make


# ---------- parsing ----------

def test_parse_euro():
    assert parse_euro("1.150 €") == 1150
    assert parse_euro("850,50 €") == 850
    assert parse_euro("") is None


def test_parse_period():
    assert parse_period("01.11.2026 - 30.04.2027") == (date(2026, 11, 1), date(2027, 4, 30))
    assert parse_period("ab 15.11.2026") == (date(2026, 11, 15), None)
    assert parse_period("sofort") == (None, None)


def test_distance_mtz_to_marienplatz_is_about_5_km():
    assert 4.5 < km_between(MTZ, (48.1374, 11.5755)) < 6


# ---------- hard rules ----------

def test_good_listing_passes(cfg):
    assert check(make(), cfg) is None


def test_rejections(cfg):
    assert "rent" in check(make(rent=900), cfg)
    assert "implausibly low" in check(make(rent=150), cfg)
    assert "km from work" in check(make(lat=MTZ[0] + 0.1), cfg)  # ~11 km north
    assert "after" in check(make(start=date(2027, 3, 1)), cfg)
    short = make(start=date.today(), end=date.today() + timedelta(days=60))
    assert "months" in check(short, cfg)
    assert check(make(rent=None), cfg) == "no price"


def test_missing_location_only_blocks_when_required(cfg):
    assert check(make(lat=None, lng=None), cfg) == "no location"
    assert check(make(lat=None, lng=None), cfg, need_location=False) is None


def test_closer_and_cheaper_ranks_first(cfg):
    near, far = make(lat=MTZ[0] + 0.005), make(lat=MTZ[0] + 0.04)
    assert score(near, cfg) < score(far, cfg)
    assert score(make(rent=500), cfg) < score(make(rent=800), cfg)


# ---------- store ----------

def test_store_round_trip_and_queue_order(cfg, store):
    store.add(make(id="far", lat=MTZ[0] + 0.04), cfg, "queued")
    store.add(make(id="near", lat=MTZ[0] + 0.005, start=date(2026, 11, 15)), cfg, "queued")
    assert [r["key"] for r in store.queue("wgg", backlog=False)] == ["wgg:near", "wgg:far"]
    back = listing_from_row(store.queue("wgg", backlog=False)[0])
    assert back.start == date(2026, 11, 15) and back.rent == 650
    assert km_to_work(back, cfg) < 1


def test_sent_today_counts_applied(cfg, store):
    store.add(make(id="a"), cfg, "queued")
    assert store.sent_today("wgg") == 0
    store.set("wgg:a", "applied", message="Hallo")
    assert store.sent_today("wgg") == 1


def test_duplicate_same_advertiser_same_district(cfg, store):
    first = make(id="a", lat=MTZ[0] + 0.01)
    store.add(first, cfg, "applied")
    store.set_contact(first.key, "Anna  K.")  # whitespace is normalised
    repost = make(id="b", lat=MTZ[0] + 0.02)
    assert store.duplicate_of(repost, "Anna K.") == first.url
    assert store.duplicate_of(make(id="c", district="Schwabing", lat=MTZ[0] + 0.03), "Anna K.") is None


def test_duplicate_same_spot_and_rent_across_sites(cfg, store):
    store.add(make(id="a"), cfg, "applied")
    same_flat = make(source="ka", id="99", url="https://example.org/ka")
    assert store.duplicate_of(same_flat, "someone else") == "https://example.org/1"
    assert store.duplicate_of(make(source="ka", id="98", rent=700), "") is None  # other rent = other flat
    rough = make(source="ka", id="97", extra={"approx": True})  # geocoded district centre: too vague to match
    assert store.duplicate_of(rough, "") is None


def test_thread_changed(store):
    assert store.thread_changed("wgg", "42", "t1", "Hallo") is None  # first time seen
    assert store.thread_changed("wgg", "42", "t1", "Hallo") is False
    assert store.thread_changed("wgg", "42", "t2", "Wann passt es?") is True
