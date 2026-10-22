"""Listing discovery over plain HTTP (no browser, no phone needed)."""
from __future__ import annotations

import html
import json
import re

import httpx

from .core import Listing, parse_euro, parse_period

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0 Safari/537.36")


def client() -> httpx.Client:
    return httpx.Client(headers={"User-Agent": UA, "Accept-Language": "de-DE,de;q=0.9,en;q=0.8"},
                        timeout=30, follow_redirects=True)


def _text(fragment: str) -> str:
    fragment = re.sub(r"<script.*?</script>", " ", fragment, flags=re.S)
    fragment = re.sub(r"<br\s*/?>", "\n", fragment)
    fragment = html.unescape(re.sub(r"<[^>]+>", " ", fragment))
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", fragment)).strip()


# ---------- flatsforfriendz ----------
# Every explore page embeds the map pins for *all* Munich offers (with coordinates),
# so one request is a full snapshot of the city.

FFF_EXPLORE = "https://flatsforfriendz.com/de/explore?city=muenchen"
FFF_OFFER = "https://flatsforfriendz.com/listing/offer/{}"


def fetch_fff(c: httpx.Client) -> list[Listing]:
    page = c.get(FFF_EXPLORE).raise_for_status().text.replace('\\"', '"').replace("\\u0026", "&")
    i = page.find('"pins":[')
    if i < 0:
        raise RuntimeError("flatsforfriendz: no pins in explore page (layout changed?)")
    pins, _ = json.JSONDecoder().raw_decode(page[i + 7:])
    out = []
    for p in pins:
        info = p.get("listing") or {}
        start, end = parse_period(info.get("period", ""))
        out.append(Listing(
            source="fff", id=p["uuid"], url=FFF_OFFER.format(p["uuid"]),
            title=f"{info.get('listingType', '')} in {info.get('district', '')}",
            district=info.get("district", ""), kind=info.get("listingType", ""),
            rent=parse_euro(info.get("price", "")), size=info.get("size", ""),
            start=start, end=end, lat=p.get("latitude"), lng=p.get("longitude"),
            extra={"exact": p.get("exact"), "furnishing": info.get("furnishing"), "period": info.get("period")},
        ))
    return out


# ---------- WG-Gesucht ----------

WGG_BASE = "https://www.wg-gesucht.de"
# München (90), WG rooms (0) + 1-room flats (1), newest first, max rent filtered server-side.
WGG_SEARCH = (WGG_BASE + "/wg-zimmer-und-1-zimmer-wohnungen-in-Muenchen.90.0+1.1.{page}.html"
              "?offer_filter=1&city_id=90&sort_order=0&noDeact=1&categories%5B%5D=0&categories%5B%5D=1&rMax={rmax}")


def fetch_wgg(c: httpx.Client, pages: int, rmax: int) -> list[Listing]:
    out, seen = [], set()
    for page in range(pages):
        doc = c.get(WGG_SEARCH.format(page=page, rmax=rmax)).raise_for_status().text
        for card in re.split(r'<a\s+id="liste-details-ad-', doc)[1:]:
            ad_id = re.match(r"(\d+)", card).group(1)
            href = re.search(r'href="([^"]+)"', card).group(1)
            if ad_id in seen or not href.startswith("/"):
                continue
            seen.add(ad_id)
            sub = re.search(r'ad-subtitle[^>]*title="([^"]+)"', card)
            parts = [p.strip() for p in html.unescape(sub.group(1)).split("·")] if sub else []
            mid = re.search(r'middle-row">(.*?)</div>\s*</div>', card, re.S)
            start, end = parse_period(_text(mid.group(1)) if mid else "")
            price = re.search(r'card-main-text">\s*([\d.]+)', card)
            size = re.search(r'size-value">([^<]+)<', card)
            title = re.search(r'aria-label="([^"]*)"', card)
            out.append(Listing(
                source="wgg", id=ad_id, url=WGG_BASE + href,
                title=html.unescape(title.group(1)) if title else "",
                kind=parts[0] if parts else "", district=parts[1] if len(parts) > 1 else "",
                rent=parse_euro(price.group(1)) if price else None,
                size=html.unescape(size.group(1)).strip() if size else "",
                start=start, end=end, extra={"street": parts[2] if len(parts) > 2 else ""},
            ))
    return out


def enrich_wgg(c: httpx.Client, l: Listing) -> Listing:
    """Fetch the ad page for coordinates, exact dates, total rent and description."""
    doc = c.get(l.url).raise_for_status().text
    m = re.search(r'"lat":([\d.]+),"lng":([\d.]+)', doc)
    if m:
        l.lat, l.lng = float(m.group(1)), float(m.group(2))
    total = re.search(r"Gesamtmiete.{0,400}?key_fact_value\">\s*([\d.]+)", doc, re.S)
    if total:
        l.rent = parse_euro(total.group(1))
    for label, attr in (("frei ab:", "start"), ("frei bis:", "end")):
        m = re.search(re.escape(label) + r".{0,300}?(\d{2}\.\d{2}\.\d{4})", doc, re.S)
        if m:
            setattr(l, attr, parse_period(m.group(1))[0])
    desc = re.search(r'<div id="ad_description_text">(.*?)<div class="row', doc, re.S)
    l.text = _text(desc.group(1))[:6000] if desc else ""
    return l
