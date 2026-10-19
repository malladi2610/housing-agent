from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .matching import evaluate
from .models import CLOSED_STAGES, DEFAULT_ROLES, ListingInput, Profile, Stage, ViewingInput


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=20)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=20000")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self):
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS listings(
              id INTEGER PRIMARY KEY,source TEXT NOT NULL,external_id TEXT NOT NULL,
              payload TEXT NOT NULL,match_result TEXT NOT NULL,stage TEXT NOT NULL DEFAULT 'new',
              version INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
              UNIQUE(source,external_id));
            CREATE TABLE IF NOT EXISTS messages(
              id INTEGER PRIMARY KEY,listing_id INTEGER NOT NULL REFERENCES listings(id),
              direction TEXT NOT NULL,kind TEXT NOT NULL DEFAULT 'initial',body TEXT NOT NULL,
              status TEXT NOT NULL,model TEXT,profile_revision INTEGER,listing_version INTEGER,
              in_reply_to INTEGER,version INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,sent_at TEXT,
              delivery_evidence TEXT,external_id TEXT UNIQUE);
            CREATE TABLE IF NOT EXISTS events(
              id INTEGER PRIMARY KEY,listing_id INTEGER REFERENCES listings(id),
              kind TEXT NOT NULL,detail TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs(
              id INTEGER PRIMARY KEY,kind TEXT NOT NULL,listing_id INTEGER REFERENCES listings(id),
              payload TEXT NOT NULL,dedupe_key TEXT UNIQUE,status TEXT NOT NULL DEFAULT 'queued',
              due_at TEXT NOT NULL,lease_until TEXT,lease_token TEXT,attempts INTEGER NOT NULL DEFAULT 0,
              error TEXT,created_at TEXT NOT NULL,finished_at TEXT);
            CREATE INDEX IF NOT EXISTS due_jobs ON jobs(status,due_at);
            CREATE TABLE IF NOT EXISTS viewings(
              id INTEGER PRIMARY KEY,listing_id INTEGER NOT NULL REFERENCES listings(id),
              starts_at TEXT NOT NULL,duration_minutes INTEGER NOT NULL,location TEXT NOT NULL,
              status TEXT NOT NULL,evidence TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS model_calls(
              id INTEGER PRIMARY KEY,role TEXT NOT NULL,model TEXT NOT NULL,status TEXT NOT NULL,
              elapsed_ms INTEGER,detail TEXT,created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS connector_state(
              name TEXT PRIMARY KEY,state TEXT NOT NULL,detail TEXT NOT NULL,last_attempt TEXT,last_success TEXT);
            CREATE TABLE IF NOT EXISTS mailbox_seen(
              mailbox TEXT NOT NULL,uid_validity TEXT NOT NULL,uid TEXT NOT NULL,created_at TEXT NOT NULL,
              PRIMARY KEY(mailbox,uid_validity,uid));
            """)
            conn.execute("INSERT OR IGNORE INTO settings VALUES('profile',?)", (Profile().model_dump_json(),))
            conn.execute("INSERT OR IGNORE INTO settings VALUES('roles',?)", (json.dumps(DEFAULT_ROLES),))

    def setting(self, key, default=None):
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, key, value):
        with self.connect() as conn:
            conn.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key,json.dumps(value)))

    def profile(self) -> Profile:
        return Profile.model_validate(self.setting("profile"))

    def save_profile(self, profile: Profile):
        with self.connect() as conn:
            current = Profile.model_validate_json(conn.execute("SELECT value FROM settings WHERE key='profile'").fetchone()[0])
            profile = profile.model_copy(update={"revision": current.revision + 1})
            conn.execute("UPDATE settings SET value=? WHERE key='profile'", (profile.model_dump_json(),))
            for row in conn.execute("SELECT id,payload,stage FROM listings").fetchall():
                match = evaluate(ListingInput.model_validate_json(row["payload"]), profile)
                stage = row["stage"]
                if stage in {"new", "matched", "needs_review"}:
                    stage = "matched" if match.status == "match" else "needs_review"
                conn.execute("UPDATE listings SET match_result=?,stage=?,updated_at=? WHERE id=?", (match.model_dump_json(),stage,now(),row["id"]))
            self._event(conn, None, "profile_updated", f"Search profile revision {profile.revision}")
        return profile

    @staticmethod
    def _event(conn, listing_id, kind, detail):
        conn.execute("INSERT INTO events(listing_id,kind,detail,created_at) VALUES(?,?,?,?)", (listing_id,kind,detail,now()))

    def event(self, listing_id, kind, detail):
        with self.connect() as conn:
            self._event(conn, listing_id, kind, detail)

    @staticmethod
    def _listing(row):
        if not row:
            raise KeyError("Listing not found")
        result = dict(row)
        result["data"] = json.loads(result.pop("payload"))
        result["match"] = json.loads(result.pop("match_result"))
        return result

    def listing(self, listing_id):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone()
        return self._listing(row)

    def listings(self, stage=None, search=""):
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM listings ORDER BY updated_at DESC,id DESC").fetchall()
        items = [self._listing(row) for row in rows]
        if stage:
            items = [r for r in items if r["stage"] == stage]
        if search:
            search = search.casefold()
            items = [r for r in items if search in json.dumps(r["data"], ensure_ascii=False).casefold()]
        return items

    def add_listing(self, data: ListingInput):
        external_id = data.external_id or (hashlib.sha256(str(data.url).encode()).hexdigest() if data.url else str(uuid.uuid4()))
        data = data.model_copy(update={"external_id": external_id})
        match = evaluate(data, self.profile())
        with self.connect() as conn:
            existing = conn.execute("SELECT * FROM listings WHERE source=? AND external_id=?", (data.source,external_id)).fetchone()
            if existing:
                return existing["id"], False
            stage = "matched" if match.status == "match" else "needs_review"
            cursor = conn.execute("INSERT INTO listings(source,external_id,payload,match_result,stage,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (data.source,external_id,data.model_dump_json(),match.model_dump_json(),stage,now(),now()))
            identifier = cursor.lastrowid
            self._event(conn, identifier, "listing_added", f"Imported from {data.source}")
        return identifier, True

    def update_listing(self, listing_id, data: ListingInput, expected_version: int):
        match = evaluate(data, self.profile())
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone()
            if not row or row["version"] != expected_version:
                raise ValueError("Listing changed; reload before saving")
            if data.source != row["source"] or data.external_id != row["external_id"]:
                raise ValueError("Source identifiers cannot be changed")
            stage = row["stage"]
            if stage in {"new", "matched", "needs_review"}:
                stage = "matched" if match.status == "match" else "needs_review"
            conn.execute("UPDATE listings SET payload=?,match_result=?,stage=?,version=version+1,updated_at=? WHERE id=?", (data.model_dump_json(),match.model_dump_json(),stage,now(),listing_id))
            self._event(conn, listing_id, "listing_updated", "Listing facts updated; existing drafts require regeneration")

    def set_stage(self, listing_id, stage: Stage):
        self.listing(listing_id)
        with self.connect() as conn:
            conn.execute("UPDATE listings SET stage=?,updated_at=? WHERE id=?", (stage.value,now(),listing_id))
            if stage in CLOSED_STAGES or stage in {Stage.VIEWING_CONFIRMED, Stage.VIEWED}:
                conn.execute("UPDATE jobs SET status='cancelled',finished_at=? WHERE listing_id=? AND kind='followup' AND status='queued'", (now(),listing_id))
            self._event(conn, listing_id, "stage_changed", stage.value)

    def messages(self, listing_id):
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM messages WHERE listing_id=? ORDER BY id", (listing_id,))]

    def message(self, message_id):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        if not row:
            raise KeyError("Message not found")
        return dict(row)

    def add_draft(self, listing_id, body, model, profile_revision, listing_version, kind="initial", in_reply_to=None):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            listing = conn.execute("SELECT version,stage FROM listings WHERE id=?", (listing_id,)).fetchone()
            current_profile = Profile.model_validate_json(conn.execute("SELECT value FROM settings WHERE key='profile'").fetchone()[0])
            if not listing or listing["version"] != listing_version or current_profile.revision != profile_revision:
                raise ValueError("Facts changed while the model was working; regenerate the draft")
            if listing["stage"] in CLOSED_STAGES or current_profile.paused or current_profile.search_complete:
                raise ValueError("Search or listing is paused/closed")
            if in_reply_to:
                latest = conn.execute("SELECT id FROM messages WHERE listing_id=? AND direction='inbound' ORDER BY id DESC LIMIT 1", (listing_id,)).fetchone()
                if not latest or latest[0] != in_reply_to:
                    raise ValueError("A newer reply arrived; regenerate the response")
            if kind == "followup":
                initial = conn.execute("SELECT id FROM messages WHERE listing_id=? AND kind='initial' AND status='sent' ORDER BY id DESC LIMIT 1", (listing_id,)).fetchone()
                if not initial or conn.execute("SELECT 1 FROM messages WHERE listing_id=? AND id>? AND (direction='inbound' OR kind='followup') LIMIT 1", (listing_id,initial[0])).fetchone():
                    raise ValueError("Follow-up is no longer due; a reply or follow-up already exists")
                if listing["stage"] in {"viewing_proposed","viewing_confirmed","viewed"}:
                    raise ValueError("A viewing is already in progress; no follow-up is due")
            conn.execute("UPDATE messages SET status='superseded' WHERE listing_id=? AND direction='outbound' AND status='draft'", (listing_id,))
            cursor = conn.execute("INSERT INTO messages(listing_id,direction,kind,body,status,model,profile_revision,listing_version,in_reply_to,created_at) VALUES(?,'outbound',?,?,'draft',?,?,?,?,?)", (listing_id,kind,body,model,profile_revision,listing_version,in_reply_to,now()))
            conn.execute("UPDATE listings SET stage='draft_ready',updated_at=? WHERE id=?", (now(),listing_id))
            self._event(conn, listing_id, "draft_created", f"{kind} draft using {model}")
            return cursor.lastrowid

    def edit_draft(self, message_id, body, version):
        if not body.strip() or len(body) > 5000:
            raise ValueError("Message must contain 1–5000 characters")
        with self.connect() as conn:
            cursor = conn.execute("UPDATE messages SET body=?,version=version+1 WHERE id=? AND version=? AND status='draft'", (body.strip(),message_id,version))
            if not cursor.rowcount:
                raise ValueError("Draft changed or was already sent; reload")

    def record_sent(self, message_id, expected_version, exact_body, acknowledged: bool):
        if not acknowledged:
            raise ValueError("Confirm that you sent this exact message on the platform")
        profile = self.profile()
        if profile.paused or profile.search_complete:
            raise ValueError("Search is paused or complete")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current_profile = Profile.model_validate_json(conn.execute("SELECT value FROM settings WHERE key='profile'").fetchone()[0])
            if current_profile.revision != profile.revision or current_profile.paused or current_profile.search_complete:
                raise ValueError("Search settings changed; reload before recording the message")
            row = conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
            if not row or row["status"] != "draft" or row["version"] != expected_version or row["body"] != exact_body:
                raise ValueError("Draft changed or was already recorded; reload")
            listing = conn.execute("SELECT * FROM listings WHERE id=?", (row["listing_id"],)).fetchone()
            if listing["stage"] in CLOSED_STAGES or row["profile_revision"] != profile.revision or row["listing_version"] != listing["version"]:
                raise ValueError("Draft is stale or listing closed; regenerate it")
            latest_inbound = conn.execute("SELECT id FROM messages WHERE listing_id=? AND direction='inbound' ORDER BY id DESC LIMIT 1", (row["listing_id"],)).fetchone()
            if latest_inbound and latest_inbound[0] != row["in_reply_to"]:
                raise ValueError("A reply arrived after this draft; review the conversation")
            conn.execute("UPDATE messages SET status='sent',sent_at=?,delivery_evidence='Owner confirmed manual platform send' WHERE id=?", (now(),message_id))
            conn.execute("UPDATE listings SET stage='message_sent',updated_at=? WHERE id=?", (now(),row["listing_id"]))
            self._event(conn, row["listing_id"], "message_sent", "Owner recorded a manual platform send")
            if row["kind"] == "initial":
                due = (datetime.now(timezone.utc) + timedelta(days=profile.followup_days)).isoformat()
                self._enqueue(conn, "followup", row["listing_id"], {"sent_message_id":message_id}, f"followup:{message_id}", due)

    def add_reply(self, listing_id, body, external_id=None):
        if not body.strip() or len(body) > 20000:
            raise ValueError("Reply must contain 1–20000 characters")
        self.listing(listing_id)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if external_id:
                prior = conn.execute("SELECT id FROM messages WHERE external_id=?", (external_id,)).fetchone()
                if prior:
                    return prior[0]
            cursor = conn.execute("INSERT INTO messages(listing_id,direction,body,status,external_id,created_at) VALUES(?,'inbound',?,'received',?,?)", (listing_id,body.strip(),external_id,now()))
            conn.execute("UPDATE messages SET status='superseded' WHERE listing_id=? AND status='draft' AND direction='outbound'", (listing_id,))
            conn.execute("UPDATE listings SET stage='response_received',updated_at=? WHERE id=? AND stage NOT IN ('secured','rejected','closed','archived')", (now(),listing_id))
            conn.execute("UPDATE jobs SET status='cancelled',finished_at=? WHERE listing_id=? AND kind='followup' AND status='queued'", (now(),listing_id))
            self._event(conn, listing_id, "reply_received", "Incoming reply recorded; pending follow-up cancelled")
            return cursor.lastrowid

    @staticmethod
    def _enqueue(conn, kind, listing_id, payload, dedupe_key, due_at):
        cursor = conn.execute("INSERT OR IGNORE INTO jobs(kind,listing_id,payload,dedupe_key,due_at,created_at) VALUES(?,?,?,?,?,?)", (kind,listing_id,json.dumps(payload),dedupe_key,due_at or now(),now()))
        if cursor.rowcount:
            return cursor.lastrowid
        return conn.execute("SELECT id FROM jobs WHERE dedupe_key=?", (dedupe_key,)).fetchone()[0]

    def enqueue(self, kind, listing_id=None, payload=None, dedupe_key=None, due_at=None):
        if kind not in {"analyze", "draft", "reply", "followup", "sync_imap", "screen"}:
            raise ValueError("Unsupported job type")
        with self.connect() as conn:
            return self._enqueue(conn,kind,listing_id,payload or {},dedupe_key or str(uuid.uuid4()),due_at)

    def claim_job(self):
        profile = self.profile()
        if profile.paused or profile.search_complete:
            return None
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM jobs WHERE (status='queued' AND due_at<=?) OR (status='running' AND lease_until<?) ORDER BY due_at,id LIMIT 1", (now(),now())).fetchone()
            if not row:
                return None
            token = uuid.uuid4().hex
            lease = (datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat()
            conn.execute("UPDATE jobs SET status='running',lease_until=?,lease_token=?,attempts=attempts+1 WHERE id=?", (lease,token,row["id"]))
            result = dict(row)
            result.update(lease_token=token,attempts=row["attempts"]+1,payload=json.loads(row["payload"]))
            return result

    def finish_job(self, job, error=None, cancelled=False):
        with self.connect() as conn:
            conn.execute("UPDATE jobs SET status=?,error=?,finished_at=?,lease_until=NULL WHERE id=? AND lease_token=? AND status='running'", ("cancelled" if cancelled else "failed" if error else "done", error,now(),job["id"],job["lease_token"]))

    def jobs(self, limit=50):
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,))]

    def events(self, listing_id=None, limit=40):
        with self.connect() as conn:
            if listing_id is None:
                rows = conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
            else:
                rows = conn.execute("SELECT * FROM events WHERE listing_id=? ORDER BY id DESC LIMIT ?", (listing_id,limit))
            return [dict(r) for r in rows]

    def connector(self, name, state, detail):
        with self.connect() as conn:
            conn.execute("INSERT INTO connector_state(name,state,detail,last_attempt,last_success) VALUES(?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET state=excluded.state,detail=excluded.detail,last_attempt=excluded.last_attempt,last_success=CASE WHEN excluded.state='ready' THEN excluded.last_success ELSE connector_state.last_success END", (name,state,detail,now(),now() if state=="ready" else None))

    def connectors(self):
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM connector_state ORDER BY name")]

    def reserve_model_call(self, role, model):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            profile = Profile.model_validate_json(conn.execute("SELECT value FROM settings WHERE key='profile'").fetchone()[0])
            if profile.paused or profile.search_complete:
                raise ValueError("Search is paused or complete")
            count = conn.execute("SELECT count(*) FROM model_calls WHERE created_at>=?", (datetime.now(timezone.utc).date().isoformat(),)).fetchone()[0]
            if count >= profile.daily_model_calls:
                raise ValueError("Daily model call limit reached; model work is paused until the next UTC day")
            return conn.execute("INSERT INTO model_calls(role,model,status,created_at) VALUES(?,?,'started',?)", (role,model,now())).lastrowid

    def finish_model_call(self, call_id, status, elapsed_ms, detail=""):
        with self.connect() as conn:
            conn.execute("UPDATE model_calls SET status=?,elapsed_ms=?,detail=? WHERE id=?", (status,elapsed_ms,detail,call_id))

    def model_stats(self):
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT role,model,count(*) AS calls,round(avg(elapsed_ms)) AS avg_ms,sum(status!='ok') AS failures FROM model_calls GROUP BY role,model")]

    def add_viewing(self, listing_id, viewing: ViewingInput):
        listing = self.listing(listing_id)
        if listing["stage"] in CLOSED_STAGES:
            raise ValueError("Reopen the listing before arranging another viewing")
        starts = viewing.starts_at.astimezone(timezone.utc)
        if starts <= datetime.now(timezone.utc):
            raise ValueError("Choose a future viewing time")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if viewing.confirmed:
                for other in conn.execute("SELECT * FROM viewings WHERE status='confirmed'"):
                    other_start = datetime.fromisoformat(other["starts_at"])
                    if starts < other_start+timedelta(minutes=other["duration_minutes"]+30) and starts+timedelta(minutes=viewing.duration_minutes+30)>other_start:
                        raise ValueError("This conflicts with a confirmed viewing or its 30-minute travel buffer")
            cursor = conn.execute("INSERT INTO viewings(listing_id,starts_at,duration_minutes,location,status,evidence,created_at) VALUES(?,?,?,?,?,?,?)", (listing_id,starts.isoformat(),viewing.duration_minutes,viewing.location,"confirmed" if viewing.confirmed else "proposed",viewing.confirmation_evidence,now()))
            stage = "viewing_confirmed" if viewing.confirmed else "viewing_proposed"
            conn.execute("UPDATE listings SET stage=?,updated_at=? WHERE id=?", (stage,now(),listing_id))
            conn.execute("UPDATE jobs SET status='cancelled',finished_at=? WHERE listing_id=? AND kind='followup' AND status='queued'", (now(),listing_id))
            self._event(conn, listing_id, stage, f"{viewing.location} · {starts.isoformat()}")
            return cursor.lastrowid

    def cancel_viewing(self, viewing_id):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            viewing = conn.execute("SELECT * FROM viewings WHERE id=?", (viewing_id,)).fetchone()
            if not viewing:
                raise KeyError("Viewing not found")
            conn.execute("UPDATE viewings SET status='cancelled' WHERE id=?", (viewing_id,))
            identifier = viewing["listing_id"]
            listing = conn.execute("SELECT * FROM listings WHERE id=?", (identifier,)).fetchone()
            active = conn.execute("SELECT 1 FROM viewings WHERE listing_id=? AND status IN ('proposed','confirmed') LIMIT 1", (identifier,)).fetchone()
            if not active and listing["stage"] in {"viewing_proposed","viewing_confirmed"}:
                inbound = conn.execute("SELECT 1 FROM messages WHERE listing_id=? AND direction='inbound' LIMIT 1", (identifier,)).fetchone()
                match = json.loads(listing["match_result"])
                stage = "response_received" if inbound else "matched" if match["status"]=="match" else "needs_review"
                conn.execute("UPDATE listings SET stage=?,updated_at=? WHERE id=?", (stage,now(),identifier))
            self._event(conn,identifier,"viewing_cancelled","Viewing cancelled; no follow-up was automatically re-enabled")
            return identifier

    def viewings(self, listing_id=None):
        with self.connect() as conn:
            if listing_id is None:
                rows = conn.execute("SELECT * FROM viewings ORDER BY starts_at")
            else:
                rows = conn.execute("SELECT * FROM viewings WHERE listing_id=? ORDER BY starts_at", (listing_id,))
            return [dict(r) for r in rows]

    def backup(self, destination: Path):
        with self.connect() as source:
            with sqlite3.connect(destination) as target:
                source.backup(target)
