"""The loop: poll -> filter -> queue -> (read, judge+write with Ollama, send) -> notify."""
from __future__ import annotations

import json
import random
import subprocess
import time
from datetime import datetime

from . import llm, more, sources
from .core import DATA, SOURCES, Listing, Store, check, km_to_work, listing_from_row, load_config, log

CFG_KEY = {"fff": "flatsforfriendz", "wgg": "wg_gesucht", "ka": "kleinanzeigen", "is24": "immoscout", "iw": "immowelt"}
ENRICH = {"wgg": sources.enrich_wgg, "ka": more.enrich_ka, "is24": more.enrich_is24, "iw": more.enrich_iw}
BROWSER = ("is24", "iw")  # discovered through the bot Chrome: polled less often


def title_key(title: str) -> str:
    """WG-Gesucht inbox rows show the ad title; its prefix links a reply to our listing."""
    return " ".join(title.split())[:30]


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
        self.next_poll: dict[str, float] = {}
        self.last_replies = 0.0

    def enabled(self, src: str) -> bool:
        return bool(self.cfg["sources"].get(CFG_KEY[src], False))

    @property
    def phone(self):
        if self._phone is None:
            from .phone import Phone
            self._phone = Phone(self.cfg["phone"].get("serial", ""))
        return self._phone

    # ---------- discovery ----------

    def poll(self) -> int:
        cfg, new = self.cfg, 0
        s, rmax = cfg["sources"], cfg["search"]["max_rent"]
        fetchers = {
            "fff": sources.fetch_fff,
            "wgg": lambda c: sources.fetch_wgg(c, s["wg_gesucht_pages"], rmax),
            "ka": lambda c: more.fetch_ka(c, rmax),
            "is24": lambda c: more.fetch_is24(s.get("immoscout_pages", 1), rmax),
            "iw": lambda c: more.fetch_iw(s.get("immowelt_pages", 1), rmax),
        }
        for src, fetch in fetchers.items():
            if not self.enabled(src) or time.time() < self.next_poll.get(src, 0):
                continue
            if src in BROWSER:
                self.next_poll[src] = time.time() + s.get("browser_poll_minutes", 15) * 60
            try:
                listings = fetch(self.http)
                first_run = not self.store.has_source(src)
                for l in listings:
                    if not self.store.known(l.key):
                        new += self._consider(l, backlog=first_run)
            except Exception as e:
                log(f"[{src}] poll failed: {e}")
                if str(e).startswith("blocked:"):  # CAPTCHA / bot check: back off, never try to solve it
                    self.next_poll[src] = time.time() + 3600
                    notify("Homeward: bot check", f"{SOURCES[src]} shows a CAPTCHA, pausing it for 1 h")
                continue
            if first_run:
                log(f"[{src}] first snapshot: {len(listings)} listings, backlog queued")
        return new

    def _consider(self, l: Listing, backlog: bool) -> int:
        extra = more.skip if l.source in ("ka", "is24", "iw") else (lambda _: None)
        reason = extra(l) or (None if l.source == "ka" else check(l, self.cfg, need_location=l.source == "fff"))
        if not reason and l.source in ENRICH:
            try:
                ENRICH[l.source](self.http, l)
                time.sleep(1)
            except Exception as e:
                if str(e).startswith("blocked:"):
                    raise
                log(f"[{l.source}] detail fetch failed for {l.id}: {e}")
                return 0  # unknown -> retry on the next poll
            reason = extra(l) or check(l, self.cfg)
        if reason:
            self.store.add(l, self.cfg, "skipped", reason, backlog)
            return 0
        self.store.add(l, self.cfg, "queued", "", backlog)
        if not backlog:
            log(f"[{l.source}] NEW match {km_to_work(l, self.cfg)} km: {l.summary()} {l.url}")
        return 1

    # ---------- applying ----------

    def work(self) -> None:
        from .dashboard import PAUSE
        if PAUSE.exists():
            return
        run = self.cfg["run"]
        allowed = run.get("auto_apply", ["fff", "wgg"])  # other enabled sources only search + queue
        for src in SOURCES:
            if not self.enabled(src) or src not in allowed or self.store.sent_today(src) >= run["daily_cap"]:
                continue
            todo = [r for r in self.store.queue(src, backlog=False) if not self._waiting(r)]
            lo, hi = run["backlog_hours"]
            due = time.time() - self.last_backlog.get(src, 0) >= run["backlog_every_minutes"] * 60
            if not todo and due and lo <= datetime.now().hour < hi:
                todo = [r for r in self.store.queue(src, backlog=True) if not self._waiting(r)][:1]
                self.last_backlog[src] = time.time()
            for row in todo:
                if self.store.sent_today(src) >= run["daily_cap"]:
                    break
                if not self.apply(row):
                    break  # source unavailable (phone locked, logged out): try again next tick

    def _waiting(self, row) -> bool:
        """ImmoScout24 lets only MieterPlus members contact during a listing's first days."""
        if row["source"] != "is24" or self.cfg["sources"].get("immoscout_mieterplus"):
            return False
        until = json.loads(row["data"])["extra"].get("paywall_until")
        try:
            return bool(until) and datetime.fromisoformat(until) > datetime.now().astimezone()
        except ValueError:
            return False

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
                    self.store.set_contact(l.key, host)
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
                {"wgg": send, "ka": more.send_ka, "is24": more.send_is24, "iw": more.send_iw}[l.source](l, msg)
                self.store.set_contact(l.key, l.id if l.source == "ka" else title_key(l.title))
            self.store.set(l.key, "applied", verdict.get("model", ""), msg)
            log(f"[{l.source}] APPLIED {km} km €{l.rent} {l.district} {l.url}")
            notify("Homeward: applied ✓", f"{km} km · €{l.rent} · {l.district}")
        except (PhoneError, WggError) as e:
            text = str(e)
            if "already contacted" in text or text.startswith(("skip:", "wait:")):
                self.store.set(l.key, "skipped", text)
            elif "not logged in" in text or text.startswith("blocked:"):
                log(f"[{l.source}] {text}")
                notify("Homeward needs you", text)
                return False
            else:
                self.store.set(l.key, "failed", text)
                log(f"[{l.source}] FAILED {l.id}: {text}")
        except llm.ModelError as e:
            log(f"[llm] {e}")  # stays queued
            return False
        except Exception as e:
            log(f"[{l.source}] error on {l.id}: {type(e).__name__}: {e}")
            if l.source == "fff":  # device disconnects etc.: keep queued, retry later
                self._phone = None
                return False
            self.store.set(l.key, "failed", f"{type(e).__name__}: {str(e)[:150]}")  # never loop on one listing
        return True

    # ---------- replies ----------

    PENDING = ("Thanks for your request", "Danke für deine Anfrage")

    def check_replies(self) -> None:
        every = self.cfg["run"].get("reply_check_minutes", 10) * 60
        if time.time() - self.last_replies < every:
            return
        self.last_replies = time.time()
        if self.cfg["sources"]["flatsforfriendz"]:
            self._replies_fff()
        if self.cfg["sources"]["wg_gesucht"]:
            self._replies_wgg()
        if self.enabled("ka"):
            self._replies_ka()

    def _replies_fff(self) -> None:
        try:
            if self.phone.ready():
                return
            first = not self.store.has_threads("fff")
            rows = self.phone.chats()
            self.phone.home()
        except Exception as e:
            log(f"[fff] reply check failed: {type(e).__name__}: {e}")
            self._phone = None
            return
        for r in rows:
            changed = self.store.thread_changed("fff", r["name"], r["when"], r["preview"])
            if first or changed is False or r["preview"].startswith(self.PENDING):
                continue
            text = r["preview"].replace("\n", " ")
            if "accepted your request" in text:
                self.store.mark_contact("fff", r["name"], "replied", "ACCEPTED - chat is open")
                log(f"[fff] ACCEPTED by {r['name']}: {text}")
                notify(f"Homeward: {r['name']} accepted!", "Open the chat in Flatsforfriendz and say hi")
                continue
            if "no longer active" in text or "nicht mehr aktiv" in text:
                self.store.mark_contact("fff", r["name"], "closed", text[:120])
                continue
            self._reply("fff", r["name"], r["name"], text)

    def _replies_wgg(self) -> None:
        from .wgg import inbox
        try:
            first = not self.store.has_threads("wgg")
            rows = inbox()
        except Exception as e:
            log(f"[wgg] reply check failed: {type(e).__name__}: {e}")
            return
        for r in rows:
            changed = self.store.thread_changed("wgg", r["id"], r["last"], r["preview"])
            if first or changed is False or r["mine"]:
                continue
            self._reply("wgg", r["name"].split(" (")[0], title_key(r["title"]), r["preview"])

    def _replies_ka(self) -> None:
        try:
            first = not self.store.has_threads("ka")
            rows = more.inbox_ka()
        except Exception as e:
            log(f"[ka] reply check failed: {type(e).__name__}: {e}")
            return
        for r in rows:
            changed = self.store.thread_changed("ka", r["id"], r["last"], r["preview"])
            if first or changed is False or r["mine"]:
                continue
            self._reply("ka", r["name"], r["ad"], r["preview"])  # contact = ad id, set when we applied

    def _reply(self, src: str, name: str, contact: str, text: str) -> None:
        n = self.store.mark_contact(src, contact, "replied", text[:200])
        log(f"[{src}] REPLY from {name}: {text[:300]}")
        notify(f"Homeward: reply from {name}", text if n else f"(not a bot application) {text}")

    # ---------- main loop ----------

    def tick(self) -> None:
        (DATA / "heartbeat").write_text(datetime.now().isoformat(timespec="seconds"))
        n = self.poll()
        if n:
            log(f"{n} new matching listing(s)")
        self.work()
        self.check_replies()

    def run(self) -> None:
        from .dashboard import PORT, serve
        serve()
        log(f"homeward started (send={'off' if self.dry else 'on'}), dashboard http://127.0.0.1:{PORT}")
        while True:
            try:
                self.tick()
            except Exception as e:
                log(f"tick error: {type(e).__name__}: {e}")
            time.sleep(self.cfg["run"]["poll_seconds"] * random.uniform(0.8, 1.2))
