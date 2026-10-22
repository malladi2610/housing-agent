"""Kleinanzeigen (plain HTTP), ImmoScout24 and immowelt (bot Chrome): discovery, detail parsing, sending."""
from __future__ import annotations

import html
import json
import re
import time
from contextlib import contextmanager
from datetime import date

import httpx

from .core import DATA, Listing, load_config, parse_euro
from .sources import _text
from .wgg import WggError, _dismiss_cookies, ensure_browser


class SiteError(WggError):
    pass


# ---------- shared ----------

SKIP = re.compile(r"tausch|wohnberechtigungsschein|\bWBS\b|housinganywhere|wunderflats|homelike|spotahome|"
                  r"uniplaces|nestpick|mr\.? ?lodge", re.I)
MONTHS = {m: i + 1 for i, m in enumerate(
    "januar februar märz april mai juni juli august september oktober november dezember".split())}


def skip(l: Listing) -> str | None:
    """Swaps, WBS-only and fee-charging platforms/agencies."""
    if l.extra.get("swap"):
        return "skip: swap (Tausch)"
    if l.extra.get("fee"):
        return "skip: brokerage/booking fee"
    m = SKIP.search(" ".join((l.title, l.kind, l.extra.get("provider", ""))))
    return f"skip: {m.group(0)}" if m else None


def _date(s: str) -> date | None:
    s = (s or "").lower()
    try:
        if m := re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})", s):
            d, mo, y = map(int, m.groups())
            return date(y + 2000 if y < 100 else y, mo, d)
        if (m := re.search(r"([a-zä]+)\s+(\d{4})", s)) and m.group(1) in MONTHS:
            return date(int(m.group(2)), MONTHS[m.group(1)], 1)
    except ValueError:
        return None
    return date.today() if "sofort" in s else None


ROOM = re.compile(r"\bWG\b|Zimmer in|Mitbewohner|FLATSHARING", re.I)


def allin(l: Listing, warm: int | None, cold: int | None) -> None:
    """All-in = warm (+ €70 power/internet for flats), or cold + €200 (estimate) if only cold rent is known."""
    room = bool(ROOM.search(f"{l.title} {l.kind}"))
    warm, cold = round(warm) if warm else None, round(cold) if cold else None
    if warm:
        l.rent, note = warm + (0 if room else 70), f"warm €{warm}" + ("" if room else " + €70 power/internet")
    elif cold:
        l.rent, note = cold + 200, f"ESTIMATE: only cold rent €{cold} known, +€200 assumed for utilities/power/internet"
    else:
        l.rent, note = None, ""
    l.extra.update(rent_note=note, estimate=not warm)


GEO, _last_geo = DATA / "geocode.json", [0.0]


def geocode(q: str) -> list[float] | None:
    """Nominatim (1 req/s, cached in data/geocode.json) for listings that only give a PLZ/district."""
    cache = json.loads(GEO.read_text()) if GEO.exists() else {}
    if q not in cache:
        time.sleep(max(0.0, 1.1 - (time.time() - _last_geo[0])))
        r = httpx.get("https://nominatim.openstreetmap.org/search", timeout=20,
                      params={"q": q, "format": "json", "limit": 1, "countrycodes": "de"},
                      headers={"User-Agent": "Homeward/1.0 (private flat search bot)"})
        _last_geo[0] = time.time()
        hits = r.raise_for_status().json()
        cache[q] = [float(hits[0]["lat"]), float(hits[0]["lon"])] if hits else None
        GEO.parent.mkdir(exist_ok=True)
        GEO.write_text(json.dumps(cache, ensure_ascii=False))
    return cache[q]


def _locate(l: Listing, q: str) -> None:
    if l.lat is None and (hit := geocode(q)):
        l.lat, l.lng = hit
        l.extra["approx"] = True


@contextmanager
def tab():
    """A fresh tab in the bot Chrome (never touches the user's tabs); closed afterwards."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        page = p.chromium.connect_over_cdp(ensure_browser()).contexts[0].new_page()
        try:
            yield page
        finally:
            page.close()


def _open(page, url: str, wait: int = 2500) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(wait)
    if (re.search(r"Roboter|robot|captcha|Access Denied|Zugriff verweigert", page.title(), re.I)
            or page.locator('iframe[src*="captcha-delivery"]').count()):
        raise SiteError(f"blocked: bot check on {url[:60]}")
    _dismiss_cookies(page)


CONFIRM_JS = """([ok, err]) => new RegExp(ok, 'i').test(document.body.innerText) ||
  [...document.querySelectorAll(err)].some(e => e.offsetParent && e.innerText.trim())"""
ALERTS_JS = """() => [...document.querySelectorAll('[role=alert], [class*=rror]')].filter(e => e.offsetParent)
  .map(e => e.innerText.trim()).filter(Boolean).join(' | ').slice(0, 200)"""


def _confirm(page, ok: str, err: str = "#no-error-box") -> None:
    """Strong success check: the site's own confirmation text must become visible (err = error-box selector)."""
    from playwright.sync_api import TimeoutError as PWTimeout
    try:
        page.wait_for_function(CONFIRM_JS, arg=[ok, err], timeout=20000)
    except PWTimeout:
        page.screenshot(path=str(DATA / "last_uncertain_send.png"), full_page=True)
        raise SiteError(f"no confirmation after send (uncertain - check the site's outbox) {page.evaluate(ALERTS_JS)}")
    if not re.search(ok, page.inner_text("body"), re.I):
        raise SiteError(f"site error after send: {page.locator(err).first.inner_text()[:150]}")


def _fill(box, message: str) -> None:
    from playwright.sync_api import TimeoutError as PWTimeout
    try:
        box.wait_for(state="visible", timeout=15000)
    except PWTimeout:
        raise SiteError("contact form did not open (layout changed or not logged in?)")
    box.fill(message)
    if box.input_value().strip()[:40] != message.strip()[:40]:
        raise SiteError("composer text mismatch, not sending")


# ---------- Kleinanzeigen (HTTP) ----------

KA = "https://www.kleinanzeigen.de"
KA_SEARCH = ("/s-auf-zeit-wg/muenchen/anzeige:angebote/preis::{rmax}/c199l6411",   # WG rooms / temporary
             "/s-wohnung-mieten/muenchen/anzeige:angebote/preis::{rmax}/c203l6411")  # flats (cold rent)


def fetch_ka(c: httpx.Client, rmax: int) -> list[Listing]:
    c.cookies.set("__ka_vip-astro-v1", "enabled", domain="www.kleinanzeigen.de")  # one stable page layout
    out, seen = [], set()
    for path in KA_SEARCH:
        doc = c.get(KA + path.format(rmax=rmax)).raise_for_status().text
        for card in re.split(r"<article\b", doc)[1:]:
            m = re.match(r'[^>]*data-adid="(\d+)"[^>]*data-href="(/s-anzeige/[^"]+)"', card)
            if not m or m.group(1) in seen:
                continue
            seen.add(m.group(1))
            t = re.search(r'"title":"((?:[^"\\]|\\.)*)"', card)
            out.append(Listing(source="ka", id=m.group(1), url=KA + m.group(2),
                               title=json.loads(f'"{t.group(1)}"') if t else "",
                               kind="WG/Zeit" if "c199" in path else "Wohnung"))
    return out


def _astro(v):
    """Decode Astro island props ([type, value] pairs)."""
    if isinstance(v, list) and len(v) == 2 and isinstance(v[0], int):
        return [_astro(x) for x in v[1]] if v[0] == 1 else _astro(v[1])
    if isinstance(v, dict):
        return {k: _astro(x) for k, x in v.items()}
    return None if isinstance(v, list) and len(v) == 1 else v


def _ka_ad(doc: str, ad_id: str) -> dict | None:
    for props in re.findall(r'<astro-island[^>]*?props="([^"]*localizedAttributes[^"]*)"', doc):
        data = _astro(json.loads(html.unescape(props)))
        stack = [data]
        while stack:
            o = stack.pop()
            if isinstance(o, dict):
                if str(o.get("id")) == ad_id and "localizedAttributes" in o:
                    return o
                stack.extend(o.values())
            elif isinstance(o, list):
                stack.extend(o)
    return None


def enrich_ka(c: httpx.Client, l: Listing) -> Listing:
    doc = c.get(l.url).raise_for_status().text
    if ad := _ka_ad(doc, l.id):
        attrs = {a["localizedName"]: a["localizedValue"] for a in ad.get("localizedAttributes") or []}
        loc, user = ad.get("locationSearchParameters") or {}, ad.get("userDetails") or {}
        lat, lng, street, place = loc.get("latitude"), loc.get("longitude"), loc.get("streetAndHouseNumber"), loc.get("location")
        price, desc, commercial = (ad.get("price") or {}).get("amount"), ad.get("description") or "", user.get("commercial")
        l.title, provider = ad.get("title") or l.title, _text((ad.get("imprint") or "").split("<br")[0])
    else:  # classic page layout
        g = lambda rx: _text(m.group(1)) if (m := re.search(rx, doc, re.S)) else None
        attrs = {_text(a): _text(b) for a, b in re.findall(
            r'addetailslist--detail">(.*?)<span class="addetailslist--detail--value"\s*>(.*?)</span>', doc, re.S)}
        lat, lng = g(r'og:latitude" content="([^"]+)'), g(r'og:longitude" content="([^"]+)')
        street, place = g(r'id="street-address"[^>]*>(.*?)</span>'), g(r'id="viewad-locality"[^>]*>(.*?)</span>')
        price, desc = parse_euro(g(r'id="viewad-price"[^>]*>(.*?)<') or ""), g(r'id="viewad-description-text"[^>]*>(.*?)</p>') or ""
        commercial, provider = "Gewerblicher" in doc, ""
    if lat and lng:
        l.lat, l.lng = float(lat), float(lng)
    l.district, l.size = place or "", attrs.get("Wohnfläche", "")
    l.kind = attrs.get("Art der Unterkunft") or attrs.get("Wohnungstyp") or l.kind
    l.start, l.end = _date(attrs.get("Verfügbar ab", "")), _date(attrs.get("Verfügbar bis", ""))
    warm = parse_euro(attrs.get("Warmmiete", ""))
    if "-199-" in l.url:  # WG room / temporary: advertised rent, like WG-Gesucht
        l.rent = warm or price
        l.extra["rent_note"] = "advertised room rent; budget is €850 ALL-IN - reject if the text shows extra costs above that"
    else:
        allin(l, warm, price)
    l.extra.update(approx=not street, commercial=bool(commercial), street=street or "", provider=provider,
                   swap=attrs.get("Tauschangebot", "Kein Tausch") != "Kein Tausch")
    facts = "; ".join(f"{k}: {v}" for k, v in attrs.items())
    l.text = (("Gewerblicher Anbieter\n" if commercial else "") + _text(desc) + f"\n\nDetails: {facts}")[:6000]
    return l


def send_ka(l: Listing, message: str) -> None:
    with tab() as page:
        _open(page, l.url)
        if (page.locator(".viewad-contact-button-login:visible").count() or "login.kleinanzeigen" in page.url
                or page.get_by_text("Einloggen", exact=True).first.is_visible()):  # classic / new layout
            raise SiteError("not logged in to Kleinanzeigen - log in once in the bot Chrome")
        btn = page.locator("#viewad-contact-button:visible")
        (btn if btn.count() else page.locator("button:visible", has_text=re.compile(r"Nachricht schreiben"))).first.click()
        page.wait_for_timeout(1500)
        # new layout: unnamed fields in a modal block; classic layout: #viewad-contact-modal-form
        form = page.locator("#viewad-contact-modal-form, div:has(textarea:visible):has(button:has-text('Nachricht senden'))").last
        me = load_config()["me"]
        for label, value in (("Vorname", me.get("first_name", "")), ("Nachname", me.get("last_name", ""))):
            field = form.get_by_label(re.compile(rf"^{label}"))  # landlord-style form: required name fields
            if field.count() and not field.first.input_value().strip():
                if not value:
                    raise SiteError(f"Kleinanzeigen form needs {label} - set [me] last_name in config.toml, not sending")
                field.first.fill(value)
        anrede = form.locator("button", has_text="Bitte wählen")  # required salutation dropdown
        if anrede.count():
            if not me.get("salutation"):
                raise SiteError("Kleinanzeigen form needs an Anrede - set [me] salutation in config.toml, not sending")
            anrede.first.click()
            page.get_by_role("option", name=me["salutation"], exact=True).first.click()
            page.wait_for_timeout(300)
        name = form.locator("input[name=contactName]").or_(form.get_by_label(re.compile(r"^(Profilname|Vorname)"))).first
        if name.count() and not name.input_value().strip():
            raise SiteError("Kleinanzeigen contact name is empty (set a profile name), not sending")
        _fill(form.locator("textarea:visible").first, message)
        form.locator("button", has_text=re.compile(r"senden", re.I)).last.click()
        # classic layout: .j-viewad-contact-success becomes visible ("Zu meinen Nachrichten")
        _confirm(page, r"Nachricht (wurde )?(erfolgreich )?(gesendet|versendet|verschickt)|Erfolgreich (gesendet|versendet)|Zu meinen Nachrichten|Successfully sent|Message sent",
                 ".j-viewad-contact-error, .ajaxform-fielderror:not(.is-hidden)")


def inbox_ka() -> list[dict]:
    """Conversation rows, taken from the JSON the messages page itself loads (no DOM scraping)."""
    found = []
    with tab() as page:
        page.on("response", lambda r: found.append(r) if re.search(r"conversations\?", r.url) else None)
        page.goto(KA + "/m-nachrichten.html", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(6000)
        if "login.kleinanzeigen" in page.url or "m-einloggen" in page.url:
            raise SiteError("not logged in to Kleinanzeigen - log in once in the bot Chrome")
        convs = []
        for r in found:
            try:
                convs += r.json().get("conversations") or []
            except Exception:
                continue
    if not found:
        raise SiteError("Kleinanzeigen inbox: no conversation data loaded (page changed?)")
    return [{"id": str(c.get("id")), "ad": str(c.get("adId", "")), "title": c.get("adTitle", ""),
             "name": c.get("partnerName") or c.get("sellerName") or c.get("buyerName") or "?",
             "last": str(c.get("receivedDate") or c.get("lastMessageDate") or ""),
             "preview": c.get("textShortTrimmed") or c.get("text") or "",
             "mine": c.get("boundness") == "OUTBOUND"} for c in convs]


# ---------- ImmoScout24 (bot Chrome) ----------

IS24 = "https://www.immobilienscout24.de"
IS24_SEARCH = IS24 + "/Suche/de/bayern/muenchen/wohnung-mieten?price=-{rmax}.0&sorting=2&pagenumber={page}"
IS24_LIST_JS = """() => { const r = IS24.resultList.resultListModel.searchResponseModel['resultlist.resultlist'];
  let e = (r.resultlistEntries[0] || {}).resultlistEntry || []; e = Array.isArray(e) ? e : [e];
  return e.map(x => { const re = x['resultlist.realEstate'], a = re.address || {}, t = re.calculatedTotalRent || {};
    return {id: re['@id'], title: re.title, a: a, cold: (re.price || {}).value, total: (t.totalRent || {}).value,
            mode: t.calculationMode, private: re.privateOffer, company: re.realtorCompanyName || '',
            size: re.livingSpace, rooms: re.numberOfRooms, paywall: re.paywallListing || null}; }); }"""
IS24_EXPOSE_JS = """() => ({q: Object.fromEntries([...document.querySelectorAll('[class*="is24qa-"]')].map(e =>
    [[...e.classList].find(c => c.startsWith('is24qa-')), e.innerText.trim()])),
  desc: [...document.querySelectorAll('.expose-description-block, .expose-description-wrapper > div')].map(e => e.innerText).join('\\n'),
  paywall: /isPaywallActive:\\s*true/.test(document.documentElement.innerHTML)})"""


def fetch_is24(pages: int, rmax: int) -> list[Listing]:
    out = []
    with tab() as page:
        for n in range(1, pages + 1):
            _open(page, IS24_SEARCH.format(rmax=rmax, page=n))
            for e in page.evaluate(IS24_LIST_JS):
                a, geo = e["a"], e["a"].get("wgs84Coordinate") or {}
                l = Listing(source="is24", id=e["id"], url=f"{IS24}/expose/{e['id']}", title=e["title"] or "",
                            district=a.get("quarter", ""), kind=f"Wohnung {e['rooms'] or ''} Zi.".strip(),
                            size=f"{e['size']} m²" if e["size"] else "", lat=geo.get("latitude"), lng=geo.get("longitude"),
                            extra={"postcode": a.get("postcode", ""), "street": a.get("description", {}).get("text", ""),
                                   "provider": "privat" if e["private"] == "true" else e["company"],
                                   "approx": a.get("preciseHouseNumber") != "true"})
                if (e["paywall"] or {}).get("active") == "true":
                    l.extra["paywall_until"] = e["paywall"].get("endDate", "")
                allin(l, e["total"] if e["mode"] == "SUMMARIZED" else None, e["cold"])
                out.append(l)
    return out


def enrich_is24(_c, l: Listing) -> Listing:
    with tab() as page:
        _open(page, l.url, 3000)
        d = page.evaluate(IS24_EXPOSE_JS)
    q = d["q"]
    facts = {q[k + "-label"]: v for k, v in q.items() if k + "-label" in q}
    if not facts:
        raise SiteError("ImmoScout24: no facts on the exposé (deactivated or layout changed?)")
    nk = facts.get("Nebenkosten", "")
    warm = parse_euro(facts.get("Gesamtmiete", "")) if re.search(r"\d", nk) else None
    allin(l, warm, parse_euro(facts.get("Kaltmiete", "")) or None)
    l.start = _date(facts.get("Bezugsfrei ab", ""))
    if not d["paywall"]:
        l.extra.pop("paywall_until", None)
    _locate(l, f"{l.extra.get('postcode', '')} {l.district}, München")
    keep = ("Typ", "Etage", "Bezugsfrei ab", "Kaltmiete", "Nebenkosten", "Heizkosten", "Gesamtmiete", "Kaution",
            "Haustiere", "Zimmer", "Wohnfläche ca.")
    info = "; ".join(f"{k}: {v}" for k, v in facts.items() if k.split(" o.")[0] in keep)
    l.text = f"Anbieter: {l.extra.get('provider', '')}\n{d['desc'].strip()}\n\nDetails: {info}"[:6000]
    return l


def send_is24(l: Listing, message: str) -> None:
    with tab() as page:
        _open(page, l.url, 3000)
        if re.search(r"isPaywallActive:\s*true", page.content()):
            raise SiteError("wait: MieterPlus-only listing (paywall still active)")
        page.locator('button[data-testid="contact-button"]').first.click()
        page.wait_for_timeout(3000)
        if "/warenkorb/" in page.url or "mieterplus" in page.url.lower():
            raise SiteError("wait: contact needs MieterPlus (redirected to checkout)")
        modal = page.locator("[class*=modalSimpleContactForm], [role=dialog]:has(textarea#message)").first
        if modal.get_by_text("Jetzt anmelden").count():
            raise SiteError("not logged in to ImmoScout24 - log in once in the bot Chrome")
        for f in ("firstName", "lastName", "emailAddress"):
            box = modal.locator(f"input[name={f}]")
            if box.count() and not box.first.input_value().strip():
                raise SiteError(f"ImmoScout24 profile field {f} empty, not sending")
        for paid in ("#depositGuarantee", "#is24-checkbox-isRelocationOfferChecked-input"):
            box = modal.locator(paid)
            if box.count() and box.first.is_checked():
                raise SiteError(f"upsell option {paid} is pre-ticked, not sending")
        _fill(modal.locator("textarea#message").first, message)
        modal.locator("button[type=submit]", has_text=re.compile("Abschicken|Senden", re.I)).first.click()
        _confirm(page, r"(Nachricht|Anfrage) (wurde )?(erfolgreich )?(gesendet|versendet|verschickt|übermittelt)|"
                       r"Vielen Dank für deine (Anfrage|Nachricht)")


# ---------- immowelt (bot Chrome) ----------

IW = "https://www.immowelt.de"
IW_SEARCH = (IW + "/classified-search?distributionTypes=Rent&estateTypes=Apartment&locations=AD08DE6345"
             "&priceMax={rmax}&order=DateDesc&page={page}")
IW_LIST_JS = """() => [...document.querySelectorAll('[data-testid="serp-core-classified-card-testid"]')].map(c => {
  const a = c.querySelector('a[data-testid="card-mfe-covering-link-testid"]');
  return [(a && a.href || '').split(/[?#]/)[0], c.innerText]; })"""
IW_EXPOSE_JS = """() => { const S = window.__UFRN_LIFECYCLE_SERVERREQUEST__ || {};
  const c = Object.values(S).map(v => v && v.data && v.data.classified).find(Boolean);
  if (!c) return null; const s = c.sections || {}, p = (c.contactSections || {}).provider || {};
  const h = s.hardFacts || {}, loc = s.location || {}, d = s.description || {};
  return {kind: h.title || '', head: d.headline || '', prices: h.prices || [],
    est: (((s.price || {}).breakdown || {}).total || {}).isEstimated,
    geo: (loc.geometry || {}).coordinates, published: loc.isAddressPublished, addr: loc.address || {},
    facts: (h.facts || []).map(f => [f.type, f.value]), fee: (c.tags || {}).hasBrokerageFee,
    provider: [p.contactCard && p.contactCard.title, p.intermediaryCard && p.intermediaryCard.title].filter(Boolean).join(' / '),
    text: [...(d.texts || []).map(t => (t.headline || '') + '\\n' + t.text),
           ((s.features || {}).details || {}).additionalText].filter(Boolean).join('\\n\\n')}; }"""


def fetch_iw(pages: int, rmax: int) -> list[Listing]:
    out = []
    with tab() as page:
        for n in range(1, pages + 1):
            _open(page, IW_SEARCH.format(rmax=rmax, page=n))
            for href, text in page.evaluate(IW_LIST_JS):
                lines = [t.strip() for t in text.split("\n") if t.strip()]
                if "/expose/" not in href:
                    continue
                kind = next((t for t in lines if "zur Miete" in t), "")
                facts = next((t for t in lines if re.search(r"\d m²|\d Zimmer", t)), "")
                price = next((t for t in lines if re.match(r"[\d.]+\s*€", t)), "")
                out.append(Listing(source="iw", id=href.rsplit("/", 1)[1], url=href, title=f"{kind} · {facts}",
                                   kind=kind, district=next((t for t in reversed(lines) if "München" in t), ""),
                                   size=next((f for f in facts.split(" · ") if "m²" in f), ""),
                                   rent=parse_euro(price),  # cold rent: a lower bound until the exposé is read
                                   start=_date(facts.split("frei ab")[-1]) if "frei ab" in facts else None,
                                   extra={"provider": next((t for t in lines if re.match(r"(Privat )?von |Angebot vom", t)), "")}))
    return out


def enrich_iw(_c, l: Listing) -> Listing:
    with tab() as page:
        _open(page, l.url, 3000)
        d = page.evaluate(IW_EXPOSE_JS)
    if not d:
        raise SiteError("immowelt: no listing data in exposé (page changed?)")
    prices = {p["label"]: parse_euro(p.get("amount") or p.get("value") or "") for p in d["prices"]}
    allin(l, None if d["est"] else prices.get("Warmmiete"), prices.get("Kaltmiete"))
    pts = [tuple(map(float, p)) for p in re.findall(r"\[(-?[\d.]+),\s*(-?[\d.]+)\]", json.dumps(d["geo"] or []))]
    if pts:  # a Point, or the district polygon when the address is hidden -> its centroid
        l.lng, l.lat = sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)
    a = d["addr"]
    l.district, l.title, l.kind = a.get("district") or l.district, d["head"] or l.title, d["kind"] or l.kind
    facts = dict(d["facts"])
    l.start = _date(facts.get("availability", "")) or l.start
    l.extra.update(approx=not d["published"], fee=bool(d["fee"]), provider=d["provider"] or l.extra.get("provider", ""),
                   swap="tausch" in l.kind.lower(), street=a.get("street", ""))
    _locate(l, f"{a.get('zipCode', '')} {l.district}, München")
    l.text = f"Anbieter: {l.extra['provider']}\n{_text(d['text'])}"[:6000]
    return l


def send_iw(l: Listing, message: str) -> None:
    with tab() as page:
        _open(page, l.url, 3000)
        form = page.locator("form:has(textarea[name=message]):visible").first
        if not form.count():
            raise SiteError("immowelt: no contact form on the exposé")
        for f in ("firstName", "lastName", "email"):
            box = form.locator(f"input[name={f}]")
            if box.count() and not box.first.input_value().strip():
                raise SiteError("not logged in to immowelt (contact details empty) - log in once in the bot Chrome")
        owner = form.locator("input[name=isOwner]")
        if owner.count() and owner.first.is_checked():
            raise SiteError("immowelt: 'Ich bin Eigentümer' is ticked, not sending")
        _fill(form.locator("textarea[name=message]").first, message)
        form.locator('button[data-testid="cdp-contact-form-submit.email"]').first.click()
        _confirm(page, r"(Nachricht|Anfrage) (wurde )?(erfolgreich )?(gesendet|versendet|verschickt|übermittelt)|"
                       r"Vielen Dank für (deine|Ihre) (Anfrage|Nachricht)")
