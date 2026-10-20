from __future__ import annotations

import signal
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .db import Store, now
from .ingestion import sync_imap
from .llm import OllamaRouter
from .models import CLOSED_STAGES, ScreenOutput
from .settings import Settings
from .workflows import Workflows
from .android import AndroidPipeline
from .android_config import load_config


class Worker:
    def __init__(self, settings: Settings, store: Store | None=None, router=None):
        settings.prepare()
        self.settings = settings
        self.store = store or Store(settings.database_path)
        self.router = router or OllamaRouter(settings,self.store)
        self.workflows = Workflows(self.store,self.router,settings.data_dir/"checkpoints.sqlite3")
        self.android = AndroidPipeline(settings, self.store)
        self.stopping = False

    def tick(self):
        profile = self.store.profile()
        self.store.connector("worker","paused" if profile.paused or profile.search_complete else "ready","Worker heartbeat; jobs persist across restarts")
        if profile.paused or profile.search_complete:
            return False
        android = load_config(self.store)
        if android.enabled:
            interval = android.poll_seconds
            bucket = int(time.time()) // interval
            with self.store.connect() as conn:
                busy = conn.execute("SELECT 1 FROM jobs WHERE kind='android_poll' AND (status='queued' OR (status='running' AND lease_until>?)) LIMIT 1", (now(),)).fetchone()
            if not busy:
                self.store.enqueue("android_poll", dedupe_key=f"android-poll:{bucket}")
        if all((self.settings.imap_host,self.settings.imap_user,self.settings.imap_password,self.settings.imap_senders)):
            interval = max(60,self.settings.imap_poll_seconds)
            bucket = int(time.time())//interval
            self.store.enqueue("sync_imap",dedupe_key=f"imap-poll:{bucket}")
        job = self.store.claim_job()
        if not job:
            return False
        if job["attempts"] > 3:
            self.store.finish_job(job,error="Job lease expired repeatedly; inspect the worker before retrying")
            return True
        try:
            if job["listing_id"] and self.store.listing(job["listing_id"])["stage"] in CLOSED_STAGES:
                self.store.finish_job(job,cancelled=True)
                return True
            if job["kind"] == "analyze":
                self.workflows.analyze(job["listing_id"],job["id"])
                updated = self.store.listing(job["listing_id"])
                self.store.set_setting(f"android_analysis:{updated['id']}", updated["version"])
                if self.store.setting("auto_draft",False) and updated["match"]["status"]=="match" and not self.store.messages(updated["id"]):
                    self.store.enqueue("draft",updated["id"],dedupe_key=f"auto-draft:{updated['id']}:{updated['version']}:{profile.revision}")
                self.android.queue_ready()
            elif job["kind"] == "draft":
                message_id = self.workflows.draft(job["listing_id"])
                self.android.queue_send(message_id)
            elif job["kind"] == "reply":
                self.workflows.reply(job["listing_id"],job["payload"]["message_id"])
            elif job["kind"] == "followup":
                result = self.followup(job)
                if result == "deferred":
                    return True
                if result == "cancelled":
                    self.store.finish_job(job,cancelled=True)
                    return True
            elif job["kind"] == "sync_imap":
                sync_imap(self.settings,self.store)
                if self.store.setting("auto_analyze",False) and self.settings.secret("OLLAMA_API_KEY"):
                    for item in self.store.listings():
                        if item["data"]["source"]!="manual" and not self.store.messages(item["id"]) and item["version"]==1:
                            self.store.enqueue("analyze",item["id"],dedupe_key=f"auto-analyze:{item['id']}:{item['version']}")
            elif job["kind"] == "screen":
                self.screen(job)
            elif job["kind"] == "android_poll":
                self.android.poll()
            elif job["kind"] == "android_pilot":
                self.android.pilot()
            elif job["kind"] == "android_capture":
                self.android.capture()
            elif job["kind"] == "android_send":
                if self.android.send(job) == "deferred":
                    return True
            self.store.finish_job(job)
        except Exception as exc:
            # Known service/domain exceptions contain sanitized, actionable text.
            safe = str(exc) if isinstance(exc,(ValueError,KeyError,RuntimeError)) else "Unexpected worker error; inspect the local service installation"
            safe = safe[:600]
            key = self.settings.secret("OLLAMA_API_KEY")
            if key:
                safe = safe.replace(key,"[redacted]")
            self.store.finish_job(job,error=safe)
            self.store.event(job["listing_id"],"job_failed",safe)
        return True

    def followup(self, job):
        listing = self.store.listing(job["listing_id"])
        messages = self.store.messages(job["listing_id"])
        sent_id = job["payload"]["sent_message_id"]
        original = self.store.message(sent_id)
        if listing["stage"] in CLOSED_STAGES or listing["stage"] in {"viewing_proposed","viewing_confirmed","viewed"}:
            return "cancelled"
        if original["status"] != "sent" or any(m["id"]>sent_id and (m["direction"]=="inbound" or m["kind"]=="followup") for m in messages):
            return "cancelled"
        profile = self.store.profile()
        local = datetime.now(ZoneInfo("Europe/Berlin"))
        if not profile.contact_start_hour <= local.hour < profile.contact_end_hour:
            next_time = local.replace(hour=profile.contact_start_hour,minute=0,second=0,microsecond=0)
            if next_time <= local:
                next_time += timedelta(days=1)
            with self.store.connect() as conn:
                conn.execute("UPDATE jobs SET status='queued',due_at=?,lease_until=NULL,lease_token=NULL,attempts=0 WHERE id=? AND lease_token=?", (next_time.astimezone(timezone.utc).isoformat(),job["id"],job["lease_token"]))
            return "deferred"
        message_id = self.workflows.draft(job["listing_id"],kind="followup")
        self.android.queue_send(message_id)
        return "done"

    def screen(self, job):
        identifier = job["payload"].get("screen_id","")
        if len(identifier)!=32 or any(ch not in "0123456789abcdef" for ch in identifier):
            raise ValueError("Invalid screenshot identifier")
        path = self.settings.data_dir/"screens"/f"{identifier}.png"
        data = path.read_bytes()
        if len(data)>5_000_000:
            raise ValueError("Screenshot too large")
        result,model = self.router.ask("interpret_screen",ScreenOutput,
            "Describe this application screenshot and propose ONE next action. Do not execute it. If the target/recipient is uncertain, or there is a login challenge or payment/document request, propose stop. Do not claim any click or send happened.",
            {"goal":job["payload"].get("goal","Inspect the rental application screen")[:1000]},image=data)
        self.store.event(job["listing_id"],"screen_interpreted",f"{model}: {result.model_dump_json()}")

    def run(self):
        def stop(*_):
            self.stopping = True
        signal.signal(signal.SIGINT,stop)
        signal.signal(signal.SIGTERM,stop)
        finished = threading.Event()
        def heartbeat():
            while not finished.wait(8):
                try:
                    profile = self.store.profile()
                    self.store.connector("worker", "paused" if profile.paused or profile.search_complete else "ready", "Background worker is running")
                except Exception:
                    pass  # A missing heartbeat expires visibly if the database is unavailable.
        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            while not self.stopping:
                did_work = self.tick()
                if not did_work:
                    time.sleep(2)
        finally:
            finished.set()
            thread.join(timeout=2)
            self.store.connector("worker","stopped","Worker stopped; queued work is retained")
