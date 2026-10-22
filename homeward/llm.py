"""One model call per listing: judge fit + write the first message (via the local Ollama daemon)."""
from __future__ import annotations

import json
import re
from datetime import date

import httpx

from .core import SOURCES, Listing

PROMPT = """You help {first_name} find a home in Munich. Read the listing, decide if it fits,
and if it does, write the first message to the advertiser.

APPLICANT (the only facts you may use; never invent anything else):
{about}

REQUIREMENTS: room in a shared flat (WG) or a studio; moving in between {target} and {latest};
staying at least {min_months} months; works at MTZ (Agnes-Pockels-Bogen 1, Moosach/Olympiapark area).
Today is {today}.

LISTING ({source}, {km} km from work):
{summary}
{title}
{text}

Reject (fit=false) if the listing: is a search ad / swap (Tausch) / commercial agency asking fees;
requires a gender, age, student status or occupation the applicant does not have; is only a short
sublet below {min_months} months; asks for money/ID before a viewing; or is clearly a scam.
If information is just missing, that is NOT a reason to reject.

The message (if fit): in the listing's language (German listing -> German, English -> English),
80-140 words, warm and natural, not salesy. Mention 1-2 concrete details from the listing so it is
clearly not a template. Say when the applicant could move in. Propose a viewing (in person or video).
If the listing asks for a keyword or specific info, include it. No placeholders like [Name], no
subject line, no emojis. Address the advertiser by first name if known. Sign with "{first_name}".

Answer with JSON only: {{"fit": true|false, "reason": "<short>", "language": "de"|"en", "message": "<text or empty>"}}"""


class ModelError(RuntimeError):
    pass


def assess(l: Listing, cfg: dict, km: float | None) -> dict:
    s, r, me = cfg["search"], cfg["run"], cfg["me"]
    prompt = PROMPT.format(
        first_name=me["first_name"], about=me["about"].strip(), target=s["target_move_in"],
        latest=s["latest_move_in"], min_months=s["min_months"], today=date.today().isoformat(),
        source=SOURCES.get(l.source, l.source), km=km,
        summary=l.summary(), title=l.title, text=l.text or "(no description available)",
    )
    last = None
    for model in (r["model"], r["model"], r["fallback_model"]):  # empty replies happen; retry once
        try:
            resp = httpx.post(f"{r['ollama_url']}/api/chat", timeout=180, json={
                "model": model, "stream": False, "format": "json",
                "messages": [{"role": "user", "content": prompt}],
            }).raise_for_status().json()
            content = resp["message"]["content"].strip().removeprefix("```json").strip("`").strip()
            out = json.loads(content)
            msg = (out.get("message") or "").strip()
            if out.get("fit") and (len(msg) < 60 or re.search(r"\[[^\]]+\]|\{[^}]+\}", msg)):
                raise ModelError(f"unusable message: {msg[:80]!r}")
            out["message"], out["model"] = msg, model
            return out
        except (httpx.HTTPError, ValueError, KeyError, ModelError) as e:
            last = e
    raise ModelError(f"models failed: {last}")
