"""The loop: poll -> filter -> queue -> (read, judge+write with Ollama, send) -> notify."""
from __future__ import annotations

import random
import subprocess
import time
from datetime import datetime

from . import llm, sources
from .core import Listing, Store, check, km_to_work, listing_from_row, load_config, log


def notify(title: str, body: str) -> None:
    body = body.replace('"', "'")[:200]
    subprocess.run(["osascript", "-e", f'display notification "{body}" with title "{title}" sound name "Glass"'],
                   check=False, capture_output=True)


class Agent:
    def __init__(self, cfg: dict | None = None, dry: bool = False):
        self.cfg = cfg or load_config()
        self.dry = dry or not self.cfg["run"]["send"]
        self.store = Store()
        self.http = sources.client()
        self._phone = None
        self.last_backlog: dict[str, float] = {}

    @property
    def phone(self):
        if self._phone is None:
            from .phone import Phone
            self._phone = Phone(self.cfg["phone"].get("serial", ""))
        return self._phone

    # ---------- discovery ----------

    def poll(self) -> int:
        cfg, new = self.cfg, 0
        batches = []
        if cfg["sources"]["flatsforfriendz"]:
            batches.append(("fff", sources.fetch_fff))
        if cfg["sources"]["wg_gesucht"]:
            batches.append(("wgg", lambda c: sources.fetch_wgg(c, cfg["sources"]["wg_gesucht_pages"],
                                                                cfg["search"]["max_rent"])))
        for src, fetch in batches:
            try:
                listings = fetch(self.http)
            except Exception as e:
                log(f"[{src}] poll failed: {e}")
                continue
            first_run = not self.store.has_source(src)
            for l in listings:
                if self.store.known(l.key):
                    continue
                new += self._consider(l, backlog=first_run)
            if first_run:
                log(f"[{src}] first snapshot: {len(listings)} listings, backlog queued")
        return new

    def _consider(self, l: Listing, backlog: bool) -> int:
        reason = check(l, self.cfg, need_location=l.source == "fff")
        if not reason and l.source == "wgg":
            try:
                sources.enrich_wgg(self.http, l)
                time.sleep(1)
            except Exception as e:
                log(f"[wgg] detail fetch failed for {l.id}: {e}")
                return 0  # unknown -> retry on the next poll
            reason = check(l, self.cfg)
        if reason:
            self.store.add(l, self.cfg, "skipped", reason, backlog)
            return 0
        self.store.add(l, self.cfg, "queued", "", backlog)
        if not backlog:
            log(f"[{l.source}] NEW match {km_to_work(l, self.cfg)} km: {l.summary()} {l.url}")
        return 1

    # ---------- applying ----------

    def work(self) -> None:
        run = self.cfg["run"]
        for src in ("fff", "wgg"):
            if self.store.sent_today(src) >= run["daily_cap"]:
                continue
            todo = self.store.queue(src, backlog=False)
            lo, hi = run["backlog_hours"]
            due = time.time() - self.last_backlog.get(src, 0) >= run["backlog_every_minutes"] * 60
            if not todo and due and lo <= datetime.now().hour < hi:
                todo = self.store.queue(src, backlog=True)[:1]
                self.last_backlog[src] = time.time()
            for row in todo:
                if self.store.sent_today(src) >= run["daily_cap"]:
                    break
                if not self.apply(row):
                    break  # source unavailable (phone locked, logged out): try again next tick

    def apply(self, row) -> bool:
        """Returns False when the source itself is unavailable."""
        from .phone import PhoneError
        from .wgg import WggError

        l, cfg = listing_from_row(row), self.cfg
        km = km_to_work(l, cfg)
        try:
            if l.source == "fff":
                if why := self.phone.ready():
                    log(f"[fff] waiting: {why}")
                    return False
                self.phone.open(l)
                l.text = self.phone.read()
                host = self.phone.host()
                if host:
                    l.text = f"Host: {host}\n{l.text}"
            verdict = llm.assess(l, cfg, km)
            if not verdict.get("fit"):
                self.store.set(l.key, "rejected", f"model: {verdict.get('reason', '')}")
                log(f"[{l.source}] rejected {l.id}: {verdict.get('reason')}")
                return True
            msg = verdict["message"]
            if self.dry:
                self.store.set(l.key, "draft", verdict.get("reason", ""), msg)
                log(f"[{l.source}] DRAFT {km} km {l.url}\n{msg}\n")
                notify("Homeward: draft ready", f"{km} km · €{l.rent} · {l.district}")
                return True
            if l.source == "fff":
                self.phone.open(l)  # back to the top of the listing before messaging
                self.phone.send(msg)
                self.phone.home()
            else:
                from .wgg import send
                send(l, msg)
            self.store.set(l.key, "applied", verdict.get("model", ""), msg)
            log(f"[{l.source}] APPLIED {km} km €{l.rent} {l.district} {l.url}")
            notify("Homeward: applied ✓", f"{km} km · €{l.rent} · {l.district}")
        except (PhoneError, WggError) as e:
            text = str(e)
            if "already contacted" in text:
                self.store.set(l.key, "skipped", text)
            elif "not logged in" in text:
                log(f"[wgg] {text}")
                notify("Homeward needs you", text)
                return False
            else:
                self.store.set(l.key, "failed", text)
                log(f"[{l.source}] FAILED {l.id}: {text}")
        except llm.ModelError as e:
            log(f"[llm] {e}")  # stays queued
            return False
        except Exception as e:  # device disconnects etc.: keep queued, retry later
            log(f"[{l.source}] error on {l.id}: {type(e).__name__}: {e}")
            self._phone = None
            return False
        return True

    # ---------- main loop ----------

    def tick(self) -> None:
        n = self.poll()
        if n:
            log(f"{n} new matching listing(s)")
        self.work()

    def run(self) -> None:
        log(f"homeward started (send={'off' if self.dry else 'on'})")
        while True:
            try:
                self.tick()
            except Exception as e:
                log(f"tick error: {type(e).__name__}: {e}")
            time.sleep(self.cfg["run"]["poll_seconds"] * random.uniform(0.8, 1.2))
