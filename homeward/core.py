"""Config, listing model, filtering/ranking and the SQLite store."""
from __future__ import annotations

import json
import math
import re
import sqlite3
import tomllib
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def load_config() -> dict:
    path = ROOT / "config.toml"
    if not path.exists():
        path = ROOT / "config.example.toml"
    return tomllib.loads(path.read_text())


@dataclass
class Listing:
    source: str          # "fff" | "wgg"
    id: str
    url: str
    title: str = ""
    district: str = ""
    kind: str = ""       # ROOM / FLAT / "3er WG" / ...
    rent: int | None = None
    size: str = ""
    start: date | None = None
    end: date | None = None
    lat: float | None = None
    lng: float | None = None
    text: str = ""       # full description when known
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.source}:{self.id}"

    def summary(self) -> str:
        period = f"{self.start or '?'} → {self.end or 'open-ended'}"
        return f"{self.kind} · {self.district} · €{self.rent} · {self.size} · {period}"


# ---------- parsing helpers ----------

DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")


def parse_period(text: str) -> tuple[date | None, date | None]:
    found = [date(int(y), int(m), int(d)) for d, m, y in DATE_RE.findall(text or "")]
    if not found:
        return None, None
    return found[0], (found[1] if len(found) > 1 else None)


def parse_euro(text: str) -> int | None:
    digits = re.sub(r"[^\d]", "", (text or "").split(",")[0])
    return int(digits) if digits else None


def km_between(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


# ---------- filter + rank ----------

def km_to_work(l: Listing, cfg: dict) -> float | None:
    if l.lat is None or l.lng is None:
        return None
    return round(km_between(tuple(cfg["search"]["workplace"]), (l.lat, l.lng)), 2)


def check(l: Listing, cfg: dict, need_location: bool = True) -> str | None:
    """Return a rejection reason, or None if the listing passes the hard rules."""
    s = cfg["search"]
    if l.rent is None:
        return "no price"
    if l.rent > s["max_rent"]:
        return f"rent €{l.rent} > €{s['max_rent']}"
    if l.rent < s["min_rent"]:
        return f"rent €{l.rent} implausibly low"
    latest = date.fromisoformat(s["latest_move_in"])
    if l.start and l.start > latest:
        return f"starts {l.start} after {latest}"
    if l.end:
        begin = max(l.start or date.today(), date.today())
        if (l.end - begin).days < s["min_months"] * 30.4:
            return f"only until {l.end} (< {s['min_months']} months)"
    km = km_to_work(l, cfg)
    if km is None:
        return "no location" if need_location else None
    if km > s["max_km"]:
        return f"{km} km from work"
    return None


def score(l: Listing, cfg: dict) -> float:
    """Lower is better. Distance dominates; then price, then move-in fit."""
    s = cfg["search"]
    km = km_to_work(l, cfg) or s["max_km"]
    target = date.fromisoformat(s["target_move_in"])
    late_days = max(0, ((l.start or target) - target).days)
    return round(km + 1.5 * (l.rent or s["max_rent"]) / s["max_rent"] + late_days / 30, 3)


# ---------- store ----------

class Store:
    def __init__(self, path: Path = DATA / "homeward.sqlite3"):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            create table if not exists listings (
              key text primary key, source text, url text, summary text,
              km real, score real, status text, reason text, message text,
              backlog integer, data text, first_seen text, updated text);
            create index if not exists by_status on listings(status, score);
            create table if not exists threads (
              source text, name text, seen_when text, preview text, updated text,
              primary key (source, name));
            """
        )
        try:
            self.db.execute("alter table listings add column contact text default ''")
        except sqlite3.OperationalError:
            pass  # already there

    def has_source(self, source: str) -> bool:
        return self.db.execute("select 1 from listings where source=? limit 1", (source,)).fetchone() is not None

    def known(self, key: str) -> bool:
        return self.db.execute("select 1 from listings where key=?", (key,)).fetchone() is not None

    def add(self, l: Listing, cfg: dict, status: str, reason: str = "", backlog: bool = False) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        data = json.dumps(asdict(l), default=str, ensure_ascii=False)
        self.db.execute(
            "insert or ignore into listings (key, source, url, summary, km, score, status, reason, message,"
            " backlog, data, first_seen, updated) values (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (l.key, l.source, l.url, l.summary(), km_to_work(l, cfg), score(l, cfg),
             status, reason, "", int(backlog), data, now, now),
        )
        self.db.commit()

    def set(self, key: str, status: str, reason: str = "", message: str | None = None) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        if message is None:
            self.db.execute("update listings set status=?, reason=?, updated=? where key=?", (status, reason, now, key))
        else:
            self.db.execute("update listings set status=?, reason=?, message=?, updated=? where key=?",
                            (status, reason, message, now, key))
        self.db.commit()

    def set_contact(self, key: str, contact: str) -> None:
        self.db.execute("update listings set contact=? where key=?", (" ".join(contact.split()), key))
        self.db.commit()

    def thread_changed(self, source: str, name: str, when: str, preview: str) -> bool | None:
        """Store the latest chat row; True if it changed, None if never seen before."""
        name = " ".join(name.split())
        old = self.db.execute("select seen_when, preview from threads where source=? and name=?",
                              (source, name)).fetchone()
        now = datetime.now().isoformat(timespec="seconds")
        self.db.execute("insert or replace into threads values (?,?,?,?,?)", (source, name, when, preview, now))
        self.db.commit()
        if old is None:
            return None
        return (old["seen_when"], old["preview"]) != (when, preview)

    def has_threads(self, source: str) -> bool:
        return self.db.execute("select 1 from threads where source=? limit 1", (source,)).fetchone() is not None

    def mark_contact(self, source: str, name: str, status: str, reason: str) -> int:
        name = " ".join(name.split())
        now = datetime.now().isoformat(timespec="seconds")
        n = self.db.execute("update listings set status=?, reason=?, updated=? where source=? and contact=?",
                            (status, reason, now, source, name)).rowcount
        self.db.commit()
        return n

    def queue(self, source: str, backlog: bool) -> list[sqlite3.Row]:
        return self.db.execute(
            "select * from listings where status='queued' and source=? and backlog=? order by score",
            (source, int(backlog)),
        ).fetchall()

    def sent_today(self, source: str) -> int:
        today = date.today().isoformat()
        return self.db.execute(
            "select count(*) from listings where source=? and status='applied' and updated>=?", (source, today)
        ).fetchone()[0]

    def rows(self, where: str = "1", args: tuple = ()) -> list[sqlite3.Row]:
        return self.db.execute(f"select * from listings where {where} order by updated desc", args).fetchall()


def listing_from_row(row: sqlite3.Row) -> Listing:
    d = json.loads(row["data"])
    for k in ("start", "end"):
        d[k] = date.fromisoformat(d[k]) if d.get(k) and d[k] != "None" else None
    return Listing(**d)


def log(msg: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    DATA.mkdir(exist_ok=True)
    with open(DATA / "homeward.log", "a") as f:
        f.write(line + "\n")
