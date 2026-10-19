from email.message import EmailMessage

from flat_agent.ingestion import canonical_listing_url, import_email


def test_alert_import_is_deduplicated_and_does_not_invent_costs(store):
    message=EmailMessage()
    message["Subject"]="A new room in Munich"
    message["From"]="alerts@example.test"
    message["Message-ID"]="<test-alert@example.test>"
    message.set_content("New offer: https://www.wg-gesucht.de/wg-zimmer-in-Muenchen.12345678.html?utm_source=email\nWarm rent: 800 EUR")
    first=import_email(message.as_bytes(),store)
    second=import_email(message.as_bytes(),store)
    assert first["created"]==1 and second["created"]==0
    listing=store.listing(first["listing_ids"][0])
    assert listing["match"]["status"]=="needs_review"
    assert listing["match"]["monthly_total"] is None
    assert "utm_source" not in listing["data"]["url"]


def test_redirects_and_nonlisting_links_are_not_followed(store):
    assert canonical_listing_url("https://wg-gesucht.de/agb.html") is None
    assert canonical_listing_url("https://evil.test/redirect?url=https://wg-gesucht.de") is None
    assert canonical_listing_url("https://wg-gesucht.de.evil.test/room.123.html") is None
    message=EmailMessage()
    message.set_content("Open https://tracker.example.test/offer/123")
    assert import_email(message.as_bytes(),store)["needs_manual_import"]
