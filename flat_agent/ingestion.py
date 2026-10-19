"""Read saved-search alerts. No website scraping or automatic platform sending."""
from __future__ import annotations

import email
import hashlib
import html
import imaplib
import re
import ssl
from email import policy
from email.utils import parseaddr
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .db import Store, now
from .models import ListingInput

PLATFORMS = {
    "wg-gesucht.de":"wg-gesucht",
    "flatsforfriendz.com":"flatsforfriendz",
    "immobilienscout24.de":"immoscout24",
    "immowelt.de":"immowelt",
}


def canonical_listing_url(value):
    try:
        parsed = urlparse(html.unescape(value).rstrip(".,;)>\"'"))
        if parsed.scheme not in {"http","https"} or parsed.username or parsed.password:
            return None
        hostname = (parsed.hostname or "").lower().removeprefix("www.")
        source = PLATFORMS.get(hostname)
        if not source:
            return None
        path = parsed.path
        patterns = {
            "wg-gesucht":r"\.[0-9]+\.html$",
            "immoscout24":r"/expose/[0-9]+",
            "immowelt":r"/(?:expose|immobilien)/[^/]+",
            "flatsforfriendz":r"/(?:[a-z]{2}/)?(?:listing|listings|offer|offers|flat|flats)/[^/]+",
        }
        if not re.search(patterns[source],path,re.I):
            return None
        query = urlencode([(k,v) for k,v in parse_qsl(parsed.query) if not k.lower().startswith(("utm_","ref","tracking"))])
        url = urlunparse(("https",parsed.netloc.lower(),path,"",query,""))
        return source,url
    except ValueError:
        return None


def email_content(raw: bytes):
    message = email.message_from_bytes(raw,policy=policy.default)
    texts, links = [], []
    parts = message.walk() if message.is_multipart() else [message]
    for part in parts:
        if part.get_content_disposition() == "attachment":
            continue
        if part.get_content_type() not in {"text/plain","text/html"}:
            continue
        try:
            content = part.get_content()
        except (UnicodeError,LookupError):
            content = (part.get_payload(decode=True) or b"").decode("utf-8",errors="replace")
        if not isinstance(content,str):
            continue
        links.extend(re.findall(r"https?://[^\s<>\"']+",content))
        if part.get_content_type() == "text/html":
            content = re.sub(r"<(script|style)\b[^>]*>[\s\S]*?</\1>","",content,flags=re.I)
            content = html.unescape(re.sub(r"<[^>]+>"," ",content))
        texts.append(content)
    return message,"\n".join(texts)[:50000],links


def import_email(raw: bytes, store: Store) -> dict:
    if len(raw) > 2_000_000:
        raise ValueError("Email exceeds the 2 MB import limit")
    message,text,links = email_content(raw)
    subject = str(message.get("Subject","Saved-search alert"))[:250] or "Saved-search alert"
    recognized = {}
    for link in links:
        result = canonical_listing_url(link)
        if result:
            recognized[result[1]] = result[0]
    identifiers, created = [], 0
    for url,source in list(recognized.items())[:20]:
        data = ListingInput(source=source,url=url,external_id=hashlib.sha256(url.encode()).hexdigest(),title=subject,description="Saved-search email; content may be incomplete. Verify against the listing.\n\n"+text[:48000])
        identifier,new = store.add_listing(data)
        identifiers.append(identifier)
        created += int(new)
        # Notifications are explicitly marked as excerpts and never treated as
        # proof that a viewing has been agreed.
        if not new and re.search(r"(?:neue nachricht|hat dir.*geschrieben|sent you a message|new message)",subject+"\n"+text,re.I):
            external = "mail:"+str(message.get("Message-ID") or hashlib.sha256(raw).hexdigest())+":"+str(identifier)
            store.add_reply(identifier,"Email notification (may be an excerpt; verify the full platform conversation):\n\n"+text[:18000],external)
    return {"listing_ids":identifiers,"created":created,"recognized_links":len(recognized),"needs_manual_import":not bool(recognized)}


def sync_imap(settings, store: Store):
    if not all((settings.imap_host,settings.imap_user,settings.imap_password,settings.imap_senders.strip())):
        store.connector("imap","not_configured","Set IMAP host, user, password and allowed sender addresses in .env. Use a dedicated alert folder.")
        raise ValueError("IMAP is not configured; an allowed-sender list is required")
    allowed = {v.strip().casefold() for v in settings.imap_senders.split(",") if v.strip()}
    imported = 0
    # No raw server errors or credential values are persisted.
    try:
        with imaplib.IMAP4_SSL(settings.imap_host,settings.imap_port,ssl_context=ssl.create_default_context(),timeout=30) as client:
            client.login(settings.imap_user,settings.imap_password)
            status,_ = client.select(settings.imap_folder,readonly=True)
            if status != "OK":
                raise ValueError("Could not select the dedicated IMAP folder")
            validity_data = client.response("UIDVALIDITY")[1]
            validity = str(validity_data[0].decode() if validity_data and validity_data[0] else "unknown")
            if validity == "unknown":
                raise ValueError("IMAP did not provide UIDVALIDITY; refusing an unsafe cursor")
            mailbox = hashlib.sha256(f"{settings.imap_host}|{settings.imap_user}|{settings.imap_folder}".encode()).hexdigest()
            status,data = client.uid("search",None,"ALL")
            if status != "OK":
                raise ValueError("IMAP search failed")
            for uid in (data[0] or b"").split()[-100:]:
                uid_text = uid.decode("ascii")
                with store.connect() as conn:
                    seen = conn.execute("SELECT 1 FROM mailbox_seen WHERE mailbox=? AND uid_validity=? AND uid=?", (mailbox,validity,uid_text)).fetchone()
                if seen:
                    continue
                status,parts = client.uid("fetch",uid,"(BODY.PEEK[])")
                if status != "OK":
                    raise ValueError("Could not read an IMAP message")
                raw = next((part[1] for part in parts if isinstance(part,tuple) and isinstance(part[1],bytes)),b"")
                header = email.message_from_bytes(raw,policy=policy.default)
                sender = parseaddr(str(header.get("From","")))[1].casefold()
                if sender in allowed:
                    result = import_email(raw,store)
                    imported += result["created"]
                    if result["needs_manual_import"]:
                        store.event(None,"email_needs_import","Allowed alert email had no recognized direct listing link; import it manually from the source")
                with store.connect() as conn:
                    conn.execute("INSERT OR IGNORE INTO mailbox_seen VALUES(?,?,?,?)", (mailbox,validity,uid_text,now()))
        store.connector("imap","ready",f"Read-only sync complete; {imported} new listings. At most the latest 100 folder messages are checked per poll.")
        return imported
    except Exception:
        store.connector("imap","error","IMAP sync failed. Check credentials, SSL access, allowed senders and the dedicated folder. No messages were marked read.")
        raise ValueError("IMAP sync failed; see the connector status and check local configuration") from None
