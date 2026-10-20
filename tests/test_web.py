from fastapi.testclient import TestClient

from flat_agent.settings import Settings
from flat_agent.web import create_app


def test_all_ui_pages_and_csrf(settings,listing_data):
    client=TestClient(create_app(settings))
    for path in ["/","/profile","/settings","/connections","/jobs","/listings/new","/android"]:
        result=client.get(path)
        assert result.status_code==200
        assert "Homeward" in result.text
    assert client.post("/api/listings",json=listing_data.model_dump(mode="json")).status_code==403
    token=client.get("/api/meta").json()["csrf_token"]
    result=client.post("/api/listings",json=listing_data.model_dump(mode="json"),headers={"X-CSRF-Token":token})
    assert result.status_code==200
    assert client.get(f"/listings/{result.json()['id']}").status_code==200
    assert client.post("/api/listings",json=listing_data.model_dump(mode="json"),headers={"X-CSRF-Token":token,"Origin":"https://evil.test"}).status_code==403


def test_secret_not_returned_and_forms_work(settings):
    app=create_app(settings)
    client=TestClient(app)
    token=client.get("/api/meta").json()["csrf_token"]
    result=client.post("/settings",data={"csrf_token":token,"ollama_key":"secret-for-test-only"},follow_redirects=False)
    assert result.status_code==303
    assert settings.secret("OLLAMA_API_KEY")=="secret-for-test-only"
    for path in ["/settings","/api/meta","/api/status","/jobs"]:
        assert "secret-for-test-only" not in client.get(path).text
    assert "secret-for-test-only" not in str(app.state.store.setting("roles"))
    created=client.post("/listings",data={"csrf_token":token,"title":"<script>alert(1)</script>","description":"An ordinary room"},follow_redirects=False)
    assert created.status_code==303
    assert "<script>alert(1)</script>" not in client.get(created.headers["location"]).text


def test_basic_auth_and_error_status(tmp_path):
    app=create_app(Settings(data_dir=tmp_path,ui_password="test-private-password",OLLAMA_API_KEY=""))
    client=TestClient(app)
    assert client.get("/").status_code==401
    client.auth=("owner","test-private-password")
    assert client.get("/").status_code==200
    token=client.get("/api/meta").json()["csrf_token"]
    result=client.post("/listings",data={"csrf_token":token,"title":""})
    assert result.status_code==400


def test_csv_escapes_formula_injection(settings):
    from flat_agent.models import ListingInput
    app=create_app(settings)
    app.state.store.add_listing(ListingInput(title="=HYPERLINK(\"bad\")"))
    response=TestClient(app).get("/export/listings.csv")
    assert "'=HYPERLINK" in response.text


def test_manual_send_reply_and_viewing_flow(settings,listing_data,ready_profile):
    from datetime import datetime,timedelta,timezone
    app=create_app(settings)
    client=TestClient(app)
    store=app.state.store
    token=client.get('/api/meta').json()['csrf_token']
    headers={'X-CSRF-Token':token}
    identifier=client.post('/api/listings',json=listing_data.model_dump(mode='json'),headers=headers).json()['id']
    listing=store.listing(identifier)
    draft=store.add_draft(identifier,'Hello, I would like to view the room.','test',store.profile().revision,listing['version'])
    sent=client.post(f'/messages/{draft}/record-sent',data={'csrf_token':token,'version':'1','body':'Hello, I would like to view the room.','acknowledged':'on'},follow_redirects=False)
    assert sent.status_code==303
    reply=client.post(f'/listings/{identifier}/replies',data={'csrf_token':token,'body':'Yes, Saturday works.'},follow_redirects=False)
    assert reply.status_code==303
    assert store.jobs()[0]['status']=='cancelled'
    start=(datetime.now(timezone.utc)+timedelta(days=10)).isoformat()
    result=client.post(f'/listings/{identifier}/viewings',data={'csrf_token':token,'starts_at':start,'duration_minutes':'30','location':'Test address','confirmed':'on','confirmation_evidence':'The advertiser accepted this exact date and time.'},follow_redirects=False)
    assert result.status_code==303
    assert 'Cancel this viewing' in client.get(f'/listings/{identifier}').text
    assert 'BEGIN:VEVENT' in client.get('/export/viewings.ics').text
    viewing_id=store.viewings(identifier)[0]['id']
    assert client.post(f'/viewings/{viewing_id}/cancel',data={'csrf_token':token},follow_redirects=False).status_code==303
    assert 'BEGIN:VEVENT' not in client.get('/export/viewings.ics').text
