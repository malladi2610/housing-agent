from fastapi.testclient import TestClient

from flat_agent.android_config import load_config
from flat_agent.web import create_app


def test_phone_controls_need_csrf_and_calibration_before_auto_enable(settings):
    app = create_app(settings)
    client = TestClient(app)
    assert client.get("/android").status_code == 200
    assert client.post("/android/jobs/capture").status_code == 403
    token = client.get("/api/meta").json()["csrf_token"]
    result = client.post("/android/config", data={"csrf_token": token, "udid": "phone-1", "package": "rental.app", "activity": ".Main", "mode": "review"}, follow_redirects=False)
    assert result.status_code == 303
    assert not load_config(app.state.store).enabled
    result = client.post("/android/config", data={"csrf_token": token, "udid": "phone-1", "package": "rental.app", "activity": ".Main", "mode": "automatic", "enabled": "on", "pilot_acknowledged": "on"})
    assert result.status_code == 400
    assert not client.get("/api/meta").json()["live_sending_enabled"]
    result = client.post("/android/jobs/capture", data={"csrf_token": token}, follow_redirects=False)
    assert result.status_code == 303
    assert app.state.store.jobs()[0]["kind"] == "android_capture"
    assert client.post("/android/pause", data={"csrf_token": token}, follow_redirects=False).status_code == 303


def test_private_phone_nodes_and_text_are_escaped(settings):
    app = create_app(settings)
    folder = settings.data_dir / "screens"
    folder.mkdir()
    identifier = "a" * 32
    (folder / f"{identifier}.xml").write_text('<hierarchy><node text="&lt;script&gt;alert(1)&lt;/script&gt;" resource-id="rental:id/body"/></hierarchy>')
    app.state.store.set_setting("android_inspection", identifier)
    response = TestClient(app).get("/android")
    assert response.status_code == 200
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text
    assert "<script>alert(1)</script>" not in response.text
