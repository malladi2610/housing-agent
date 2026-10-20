import json

import httpx
import pytest

from flat_agent.android_config import AndroidConfig, Selector
from flat_agent.android_phone import AppiumPhone, CalibrationRequired, PhoneInUse, parse_source, source_nodes


def test_ui_source_rejects_entity_payloads_and_filters_other_apps():
    with pytest.raises(CalibrationRequired):
        parse_source('<!DOCTYPE x [<!ENTITY payload "x">]><hierarchy/>')
    xml = '<hierarchy><node package="rental.app" text="A room" resource-id="ad"/><node package="other.app" text="Private other app"/></hierarchy>'
    assert source_nodes(xml, "rental.app") == [{"text": "A room", "resource-id": "ad", "content-desc": "", "class": "", "bounds": ""}]


def test_existing_session_never_takes_over_another_foreground_app(settings, store):
    config = AndroidConfig(udid="usb-1", package="rental.app", activity=".Main")
    store.set_setting("android_session", {"id": "session-1", "fingerprint": config.fingerprint()})
    calls = []
    def transport(request):
        calls.append((request.method, request.url.path))
        return httpx.Response(200, json={"value": "personal.messaging.app"})
    client = httpx.Client(transport=httpx.MockTransport(transport))
    phone = AppiumPhone(settings, store, config, client=client)
    with pytest.raises(PhoneInUse):
        phone.connect()
    assert calls == [("GET", "/session/session-1/appium/device/current_package")]


def test_session_creation_selects_one_phone_without_reset_or_auto_launch(settings, store):
    requests = []
    config = AndroidConfig(udid="usb-physical-phone", package="rental.app", activity=".Main")
    def transport(request):
        requests.append(request)
        if request.url.path == "/session":
            return httpx.Response(200, json={"value": {"sessionId": "new-session", "capabilities": {"udid": "usb-physical-phone"}}})
        return httpx.Response(200, json={"value": "rental.app"})
    phone = AppiumPhone(settings, store, config, client=httpx.Client(transport=httpx.MockTransport(transport)))
    phone.connect()
    caps = json.loads(requests[0].content)["capabilities"]["alwaysMatch"]
    assert caps["appium:udid"] == "usb-physical-phone"
    assert caps["appium:noReset"] is True and caps["appium:autoLaunch"] is False
    assert "appium:autoGrantPermissions" not in caps


def test_unicode_composer_and_durable_intent_precede_the_send_click(settings, store):
    config = AndroidConfig(package="rental.app", selectors={r: Selector(value=r) for r in ("thread_identity", "composer", "send", "outgoing")})
    body = "Hallo Mira, schöne Küche – ich würde gern besichtigen. Grüße, Alex"
    state = {"body": "", "intent": False, "clicked": False}
    def transport(request):
        path = request.url.path
        if path.endswith("current_package"): value = "rental.app"
        elif path.endswith("/source"): value = '<hierarchy><node package="rental.app" text="Room 123"/></hierarchy>'
        elif path.endswith("/elements"):
            role = json.loads(request.content)["value"]
            value = [{"element-6066-11e4-a52e-4f735466cecf": role}]
        elif path.endswith("thread_identity/text"): value = "Room 123"
        elif path.endswith("composer/text"): value = state["body"]
        elif path.endswith("composer/value"):
            payload = json.loads(request.content)
            assert payload == {"text": body, "value": [body]}
            state["body"] = payload["text"]; value = None
        elif path.endswith("/attribute/enabled"): value = state.get("enabled", True)
        elif path.endswith("send/click"):
            assert state["intent"] and state["body"] == body
            state["clicked"] = True; value = None
        elif path.endswith("outgoing/text"): value = body
        else: raise AssertionError(path)
        return httpx.Response(200, json={"value": value})
    phone = AppiumPhone(settings, store, config, client=httpx.Client(transport=httpx.MockTransport(transport)))
    phone.session = "session-1"
    phone.compose("Room 123", body)
    phone.click_send("Room 123", body, lambda: state.update(intent=True))
    assert state["clicked"]
    assert phone.proof("Room 123", body)["thread"] == "Room 123"
    state.update(enabled=False, clicked=False)
    with pytest.raises(CalibrationRequired, match="disabled"):
        phone.click_send("Room 123", body, lambda: pytest.fail("Disabled control must not reserve a new send intent"))
    assert not state["clicked"]


def test_invalid_selectors_need_calibration_instead_of_endless_network_retry(settings, store):
    transport = httpx.MockTransport(lambda request: httpx.Response(400, json={"value": {"error": "invalid selector", "message": "untrusted details"}}))
    phone = AppiumPhone(settings, store, AndroidConfig(), client=httpx.Client(transport=transport))
    with pytest.raises(CalibrationRequired, match="selector"):
        phone.call("POST", "/session/test/elements", {"using": "xpath", "value": "bad"})


def test_calibration_changes_reuse_the_owned_phone_session(settings, store):
    config = AndroidConfig(udid="usb-1", package="rental.app", activity=".Main")
    calls = []
    def transport(request):
        calls.append((request.method, request.url.path))
        value = {"sessionId": "owned-session", "capabilities": {"udid": "usb-1"}} if request.url.path == "/session" else "rental.app"
        return httpx.Response(200, json={"value": value})
    client = httpx.Client(transport=httpx.MockTransport(transport))
    AppiumPhone(settings, store, config, client=client).connect()
    config.selectors["title"] = Selector(value="newly-calibrated-title")
    AppiumPhone(settings, store, config, client=client).connect()
    assert calls.count(("POST", "/session")) == 1
    assert not any(method == "DELETE" for method, _ in calls)


def test_switching_device_closes_only_the_recorded_old_session(settings, store):
    store.set_setting("android_session", {"id": "our-old-session", "signature": "old-device", "endpoint": "http://127.0.0.1:4723"})
    config = AndroidConfig(udid="new-usb-phone", package="rental.app", activity=".Main")
    calls = []
    def transport(request):
        calls.append((request.method, request.url.path))
        value = {"sessionId": "new-session", "capabilities": {"udid": "new-usb-phone"}} if request.url.path == "/session" else "rental.app"
        return httpx.Response(200, json={"value": value})
    AppiumPhone(settings, store, config, client=httpx.Client(transport=httpx.MockTransport(transport))).connect()
    assert calls[:2] == [("DELETE", "/session/our-old-session"), ("POST", "/session")]
