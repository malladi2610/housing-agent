"""Site-specific parsing and the safety rules around sending, the Ollama call, and the dashboard.
Network calls are faked with httpx.MockTransport / monkeypatch, so nothing leaves the machine."""
import json
from datetime import date

import httpx
import pytest

from homeward import dashboard, llm, more, sources
from homeward.wgg import chrome_path, message_url

from conftest import make


def test_fff_reads_all_pins_from_the_explore_page():
    pins = [{"uuid": "abc", "latitude": 48.18, "longitude": 11.53, "exact": True,
             "listing": {"listingType": "ROOM", "district": "Moosach", "price": "700 €",
                         "period": "01.11.2026 - 31.10.2027", "size": "14 m²"}}]
    page = f'<script>self.__next_f.push("{json.dumps({"pins": pins}, separators=(",", ":"))[1:-1]}")</script>'.replace('"', '\\"')
    c = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, text=page)))
    [l] = sources.fetch_fff(c)
    assert (l.key, l.rent, l.start, l.lat) == ("fff:abc", 700, date(2026, 11, 1), 48.18)
    assert l.url.endswith("/listing/offer/abc")


def test_wgg_message_url():
    assert message_url(make(url="https://www.wg-gesucht.de/wg-zimmer-in-Muenchen.123.html")) == \
        "https://www.wg-gesucht.de/nachricht-senden/wg-zimmer-in-Muenchen.123.html"


def test_skip_swaps_and_fee_platforms():
    assert more.skip(make(title="Wohnungstausch Maxvorstadt")).startswith("skip:")
    assert more.skip(make(extra={"provider": "HousingAnywhere"})).startswith("skip:")
    assert more.skip(make(extra={"fee": True})) == "skip: brokerage/booking fee"
    assert more.skip(make()) is None


def test_allin_rent():
    flat = make(title="2-Zimmer Wohnung", kind="Wohnung")
    more.allin(flat, warm=700, cold=None)
    assert flat.rent == 770  # flats: + €70 power/internet
    room = make(title="Zimmer in 3er WG", kind="WG")
    more.allin(room, warm=600, cold=None)
    assert room.rent == 600  # WG rooms are usually all-in already
    cold_only = make(kind="Wohnung")
    more.allin(cold_only, warm=None, cold=550)
    assert cold_only.rent == 750 and cold_only.extra["estimate"]


def test_german_dates():
    assert more._date("01.12.26") == date(2026, 12, 1)
    assert more._date("Dezember 2026") == date(2026, 12, 1)
    assert more._date("") is None


def test_chrome_is_found():
    """CI runners have Chrome installed; a failure here means the lookup misses this OS's install path."""
    assert chrome_path()


# ---------- Ollama ----------

def fake_ollama(monkeypatch, *replies):
    """Make httpx.post return the given model replies in order; returns the list of models asked."""
    asked, replies = [], list(replies)

    def post(url, json, timeout):
        asked.append(json["model"])
        return httpx.Response(200, json={"message": {"content": replies.pop(0)}}, request=httpx.Request("POST", url))
    monkeypatch.setattr(llm.httpx, "post", post)
    return asked


def test_llm_returns_message(cfg, monkeypatch):
    msg = "Hallo Anna, " + "ich interessiere mich sehr für das Zimmer in Moosach. " * 3
    fake_ollama(monkeypatch, json.dumps({"fit": True, "reason": "ok", "language": "de", "message": msg}))
    out = llm.assess(make(), cfg, 1.1)
    assert out["fit"] and out["message"] == msg.strip() and out["model"] == cfg["run"]["model"]


def test_llm_never_sends_placeholders(cfg, monkeypatch):
    bad = json.dumps({"fit": True, "message": "Hallo [Name], " + "ich interessiere mich für das Zimmer. " * 3})
    asked = fake_ollama(monkeypatch, bad, bad, bad)
    with pytest.raises(llm.ModelError):
        llm.assess(make(), cfg, 1.1)
    assert asked == [cfg["run"]["model"], cfg["run"]["model"], cfg["run"]["fallback_model"]]


# ---------- dashboard ----------

def test_dashboard_renders(cfg, store, monkeypatch, tmp_path):
    store.add(make(title="Zimmer <nahe> MTZ"), cfg, "applied")
    monkeypatch.setattr(dashboard, "Store", lambda: store)
    monkeypatch.setattr(dashboard, "DATA", tmp_path)
    monkeypatch.setattr(dashboard, "PAUSE", tmp_path / "paused")
    page = dashboard.render()
    assert "Sending is on" in page and "Zimmer &lt;nahe&gt; MTZ" in page  # titles are HTML-escaped


# ---------- robot checks ----------

class FakePage:
    """Shows a robot check for the first `solved_after` looks, then the real page."""
    def __init__(self, solved_after):
        self.looks, self.solved_after = 0, solved_after

    def title(self):
        self.looks += 1
        return "Ich bin kein Roboter" if self.looks <= self.solved_after else "Wohnung in Moosach"

    def locator(self, _):
        return type("L", (), {"count": lambda _self: 0})()

    def wait_for_timeout(self, _):
        pass

    def bring_to_front(self):
        pass


def test_robot_check_waits_for_the_user(monkeypatch):
    monkeypatch.setattr(more, "notify", lambda *a: None)
    monkeypatch.setattr(more, "load_config", lambda: {"run": {"captcha_wait_minutes": 1}})
    page = FakePage(solved_after=3)
    more._wait_for_human(page, "https://www.immobilienscout24.de/expose/1")  # returns once solved
    assert page.looks == 4


def test_unsolved_robot_check_backs_off(monkeypatch):
    monkeypatch.setattr(more, "notify", lambda *a: None)
    monkeypatch.setattr(more, "load_config", lambda: {"run": {"captcha_wait_minutes": 0}})
    with pytest.raises(more.SiteError, match="^blocked:"):
        more._wait_for_human(FakePage(solved_after=99), "https://www.immobilienscout24.de/expose/1")
