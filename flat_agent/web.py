from __future__ import annotations

import base64
import csv
import html
import io
import json
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urlparse
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .connectors import AndroidInspector, DesktopInspector
from .db import Store
from .ingestion import canonical_listing_url, import_email
from .llm import ModelError, OllamaRouter
from .models import DEFAULT_ROLES, ListingInput, Profile, Stage, ViewingInput
from .settings import Settings
from .android import AndroidPipeline
from .android_config import AndroidConfig, SCAN_ROLES, SEND_ROLES, OPTIONAL_ROLES, load_config, save_config
from .android_phone import adb_devices, adb_foreground, source_nodes
from .android_store import DeviceLedger
from . import __version__

ROOT = Path(__file__).resolve().parent
NUMBERS = {"warm_rent","electricity","internet","other_monthly","all_in_rent","distance_km","cycling_minutes","transit_minutes"}
DATES = {"available_from","available_until"}
BOOLS = {"all_costs_confirmed","move_in_confirmed"}
STAGE_LABELS = {s.value:s.value.replace("_"," ").capitalize() for s in Stage}


def redirect(path="/", notice=""):
    return RedirectResponse(path+("?"+urlencode({"notice":notice}) if notice else ""),status_code=303)


def create_app(settings: Settings | None=None) -> FastAPI:
    settings = settings or Settings()
    settings.prepare()
    if settings.host not in {"127.0.0.1","localhost","::1"} and not settings.ui_password:
        raise ValueError("Set FLAT_UI_PASSWORD before binding the UI outside localhost")
    store = Store(settings.database_path)
    router = OllamaRouter(settings,store)
    app = FastAPI(title="Homeward · Munich",docs_url=None,redoc_url=None,openapi_url=None)
    app.state.store, app.state.settings = store, settings
    templates = Jinja2Templates(directory=str(ROOT/"templates"))

    @app.middleware("http")
    async def private_access(request: Request, call_next):
        if request.url.path != "/healthz":
            if settings.ui_password:
                authorization = request.headers.get("authorization","")
                try:
                    scheme,encoded = authorization.split(" ",1)
                    username,password = base64.b64decode(encoded,validate=True).decode("utf-8").split(":",1)
                    valid = scheme.lower()=="basic" and secrets.compare_digest(username.encode(),b"owner") and secrets.compare_digest(password.encode(),settings.ui_password.encode())
                except (ValueError,UnicodeError):
                    valid = False
                if not valid:
                    return JSONResponse({"detail":"Private tracker: sign in as owner"},status_code=401,headers={"WWW-Authenticate":'Basic realm="Homeward", charset="UTF-8"'})
            elif request.client and request.client.host not in {"127.0.0.1","::1","localhost","testclient"}:
                return JSONResponse({"detail":"Local access only. Configure FLAT_UI_PASSWORD for private remote access."},status_code=403)
        if request.method in {"POST","PUT","PATCH","DELETE"}:
            origin = request.headers.get("origin")
            if origin and (origin=="null" or urlparse(origin).netloc != request.headers.get("host")):
                return JSONResponse({"detail":"Cross-origin action rejected"},status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; form-action 'self'; frame-ancestors 'none'"
        return response

    async def mutation(request: Request):
        token = request.headers.get("x-csrf-token","")
        if not token and request.headers.get("content-type","").startswith(("application/x-www-form-urlencoded","multipart/form-data")):
            form = await request.form()
            token = str(form.get("csrf_token",""))
        if not secrets.compare_digest(token.encode(),settings.csrf_token().encode()):
            from fastapi import HTTPException
            raise HTTPException(status_code=403,detail="Missing or invalid action token; reload the page")

    def render(request, name, **context):
        profile = store.profile()
        connections = {r["name"]:r for r in store.connectors()}
        heartbeat = connections.get("worker")
        worker_ready = bool(heartbeat and heartbeat["last_attempt"] and (datetime.now(timezone.utc)-datetime.fromisoformat(heartbeat["last_attempt"])).total_seconds()<30)
        return templates.TemplateResponse(request=request,name=name,context={
            "request":request,"profile":profile,"csrf":settings.csrf_token(),"stage_labels":STAGE_LABELS,
            "notice":request.query_params.get("notice",""),"worker_ready":worker_ready,
            "key_configured":bool(settings.secret("OLLAMA_API_KEY")),"connections":connections,**context})

    @app.exception_handler(KeyError)
    async def missing(request,exc):
        return HTMLResponse("<h1>Not found</h1><p>The requested record does not exist.</p><a href='/'>Back to tracker</a>",status_code=404)

    @app.exception_handler(ValueError)
    @app.exception_handler(ModelError)
    async def invalid(request,exc):
        detail = str(exc)[:2000]
        key = settings.secret("OLLAMA_API_KEY")
        if key:
            detail = detail.replace(key,"[redacted]")
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail":detail},status_code=400)
        response = render(request,"error.html",error=detail)
        response.status_code = 400
        return response

    app.mount("/static",StaticFiles(directory=str(ROOT/"static")),name="static")

    @app.get("/healthz")
    def health():
        with store.connect() as conn:
            conn.execute("SELECT 1")
        return {"status":"ok","version":__version__}

    @app.get("/")
    def dashboard(request: Request):
        items = store.listings(request.query_params.get("stage"),request.query_params.get("q",""))
        sort = request.query_params.get("sort","recent")
        if sort=="cost":
            items.sort(key=lambda x:(x["match"]["monthly_total"] is None,x["match"]["monthly_total"] or 0))
        elif sort in {"cycling","transit","distance"}:
            field = {"cycling":"cycling_minutes","transit":"transit_minutes","distance":"distance_km"}[sort]
            items.sort(key=lambda x:(x["data"].get(field) is None,x["data"].get(field) or 0))
        all_items = store.listings()
        stats = {
            "total":len(all_items),
            "matches":sum(x["match"]["status"]=="match" for x in all_items),
            "responses":sum(x["stage"]=="response_received" for x in all_items),
            "viewings":sum(x["status"]=="confirmed" and datetime.fromisoformat(x["starts_at"])>datetime.now(timezone.utc) for x in store.viewings()),
        }
        return render(request,"dashboard.html",items=items,stats=stats,events=store.events(limit=8),sort=sort)

    @app.get("/listings/new")
    def new_listing(request: Request):
        return render(request,"import.html")

    def read_listing_form(form, original=None):
        payload = dict(original or {})
        allowed = set(ListingInput.model_fields)
        for key in allowed.intersection(form.keys()):
            value = str(form[key]).strip()
            if key in NUMBERS:
                payload[key] = float(value) if value else None
            elif key in DATES:
                payload[key] = value or None
            elif key in BOOLS:
                payload[key] = value in {"on","true","1"}
            elif key == "url":
                payload[key] = value or None
            else:
                payload[key] = value
        for key in BOOLS:
            payload[key] = key in form
        return ListingInput.model_validate(payload)

    @app.post("/listings",dependencies=[Depends(mutation)])
    async def add_listing(request: Request):
        form = await request.form()
        data = read_listing_form(form)
        if data.source=="manual" and data.url:
            recognized = canonical_listing_url(str(data.url))
            if recognized:
                data = ListingInput.model_validate({**data.model_dump(mode="json"),"source":recognized[0],"url":recognized[1]})
        identifier,created = store.add_listing(data)
        if created:
            fields = [k for k in ListingInput.model_fields if k in form and str(form[k]).strip() not in {"","unknown"}]
            store.set_setting(f"manual_fields:{identifier}",fields)
        if store.setting("auto_analyze",False) and settings.secret("OLLAMA_API_KEY"):
            store.enqueue("analyze",identifier,dedupe_key=f"auto-analyze:{identifier}:{store.listing(identifier)['version']}")
        return redirect(f"/listings/{identifier}","Listing added" if created else "This listing is already tracked")

    @app.post("/import/email",dependencies=[Depends(mutation)])
    async def email_import(request: Request):
        form = await request.form()
        upload = form.get("email_file")
        if not upload or not hasattr(upload,"read"):
            raise ValueError("Choose an .eml alert email")
        raw = await upload.read(2_000_001)
        result = import_email(raw,store)
        if store.setting("auto_analyze",False) and settings.secret("OLLAMA_API_KEY"):
            for identifier in result["listing_ids"]:
                store.enqueue("analyze",identifier,dedupe_key=f"auto-analyze:{identifier}:{store.listing(identifier)['version']}")
        if result["needs_manual_import"]:
            return redirect("/listings/new","No direct listing links recognized. Paste the listing text instead; tracking/redirect links are not followed.")
        return redirect("/",f"Imported {result['created']} new listing(s); duplicates skipped")

    @app.get("/listings/{listing_id:int}")
    def detail(request: Request,listing_id: int):
        with store.connect() as conn:
            android_listing = bool(conn.execute("SELECT 1 FROM android_ads WHERE listing_id=?", (listing_id,)).fetchone())
        return render(request,"detail.html",listing=store.listing(listing_id),messages=store.messages(listing_id),events=store.events(listing_id),viewings=store.viewings(listing_id),android_listing=android_listing)

    @app.post("/listings/{listing_id}/facts",dependencies=[Depends(mutation)])
    async def facts(request: Request,listing_id: int):
        form = await request.form()
        current = store.listing(listing_id)
        data = read_listing_form(form,current["data"])
        store.update_listing(listing_id,data,int(str(form["version"])))
        store.set_setting(f"manual_fields:{listing_id}",list(set(store.setting(f"manual_fields:{listing_id}",[]))|set(ListingInput.model_fields).intersection(form.keys())|BOOLS))
        AndroidPipeline(settings, store).queue_ready()
        return redirect(f"/listings/{listing_id}","Facts saved and eligibility recalculated")

    @app.post("/listings/{listing_id}/jobs/{kind}",dependencies=[Depends(mutation)])
    def queue_job(listing_id: int,kind: str):
        if kind not in {"analyze","draft"}:
            raise ValueError("Unsupported action")
        store.listing(listing_id)
        store.enqueue(kind,listing_id)
        return redirect(f"/listings/{listing_id}",f"{kind.capitalize()} queued. The worker will process it when active.")

    @app.post("/listings/{listing_id}/stage",dependencies=[Depends(mutation)])
    async def stage(request: Request,listing_id: int):
        form = await request.form()
        selected = Stage(str(form["stage"]))
        if selected in {Stage.VIEWING_CONFIRMED,Stage.MESSAGE_SENT}:
            raise ValueError("Record a confirmed viewing or the exact sent message using its dedicated form")
        store.set_stage(listing_id,selected)
        return redirect(f"/listings/{listing_id}","Progress updated")

    @app.post("/listings/{listing_id}/replies",dependencies=[Depends(mutation)])
    async def reply(request: Request,listing_id: int):
        form = await request.form()
        identifier = store.add_reply(listing_id,str(form.get("body","")))
        if settings.secret("OLLAMA_API_KEY"):
            store.enqueue("reply",listing_id,{"message_id":identifier},dedupe_key=f"reply:{identifier}")
        return redirect(f"/listings/{listing_id}","Reply recorded; pending follow-up cancelled")

    @app.post("/messages/{message_id}/edit",dependencies=[Depends(mutation)])
    async def edit_message(request: Request,message_id: int):
        form = await request.form()
        message = store.message(message_id)
        store.edit_draft(message_id,str(form.get("body","")),int(str(form["version"])))
        return redirect(f"/listings/{message['listing_id']}","Draft updated")

    @app.post("/messages/{message_id}/record-sent",dependencies=[Depends(mutation)])
    async def record_sent(request: Request,message_id: int):
        form = await request.form()
        message = store.message(message_id)
        store.record_sent(message_id,int(str(form["version"])),str(form.get("body","")),"acknowledged" in form)
        return redirect(f"/listings/{message['listing_id']}","Manual platform send recorded. No message was sent by this website.")

    @app.post("/listings/{listing_id}/viewings",dependencies=[Depends(mutation)])
    async def viewing(request: Request,listing_id: int):
        form = await request.form()
        selected = datetime.fromisoformat(str(form["starts_at"]))
        if selected.tzinfo is None:
            selected = selected.replace(tzinfo=ZoneInfo("Europe/Berlin"))
        data = ViewingInput(starts_at=selected,duration_minutes=int(str(form.get("duration_minutes",30))),location=str(form.get("location","")),confirmed="confirmed" in form,confirmation_evidence=str(form.get("confirmation_evidence","")))
        store.add_viewing(listing_id,data)
        return redirect(f"/listings/{listing_id}","Viewing recorded in Europe/Berlin time")

    @app.get("/profile")
    def profile_page(request: Request):
        return render(request,"profile.html")

    @app.post("/viewings/{viewing_id}/cancel",dependencies=[Depends(mutation)])
    def cancel_viewing(viewing_id: int):
        identifier = store.cancel_viewing(viewing_id)
        return redirect(f"/listings/{identifier}","Viewing cancelled. You can now record a replacement appointment.")

    @app.post("/profile",dependencies=[Depends(mutation)])
    async def save_profile(request: Request):
        form = await request.form()
        payload = store.profile().model_dump(mode="json")
        for key in Profile.model_fields:
            if key in form and key not in {"revision","home_types","paused","search_complete"}:
                payload[key] = str(form[key]).strip()
        payload["home_types"] = form.getlist("home_types")
        store.save_profile(Profile.model_validate(payload))
        return redirect("/profile","Profile saved. Listing eligibility has been recalculated; regenerate existing drafts.")

    @app.post("/control",dependencies=[Depends(mutation)])
    async def control(request: Request):
        form = await request.form()
        action = str(form.get("action",""))
        profile = store.profile()
        if action == "pause":
            profile.paused = True
        elif action == "resume":
            profile.paused,profile.search_complete = False,False
        elif action == "complete":
            profile.search_complete = True
        else:
            raise ValueError("Unknown agent control")
        store.save_profile(profile)
        return redirect("/","Search control updated")

    @app.get("/settings")
    def settings_page(request: Request):
        return render(request,"settings.html",roles=store.setting("roles"),catalog=store.setting("catalog",[]),stats=store.model_stats(),auto_analyze=store.setting("auto_analyze",False),auto_draft=store.setting("auto_draft",False),fallbacks_enabled=store.setting("fallbacks_enabled",False),ollama_blocked=store.setting("ollama_blocked",False))

    @app.post("/settings",dependencies=[Depends(mutation)])
    async def save_settings(request: Request):
        form = await request.form()
        key = str(form.get("ollama_key","")).strip()
        if key:
            settings.save_secret("OLLAMA_API_KEY",key)
        roles = store.setting("roles")
        catalog = {m["name"]:set(m["capabilities"]) for m in store.setting("catalog",[])}
        for role in DEFAULT_ROLES:
            model = str(form.get(role,roles[role]["model"])).strip()
            fallback = str(form.get(role+"_fallback",roles[role].get("fallback_model") or "")).strip()
            for name in filter(None,[model,fallback]):
                if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:\-]{0,119}",name):
                    raise ValueError("Invalid model identifier")
                if catalog and (name not in catalog or not set(roles[role]["required_capabilities"]).issubset(catalog[name])):
                    raise ValueError(f"{name} is missing this role's required cloud capabilities")
            roles[role].update(model=model,fallback_model=fallback or None)
        if ("auto_analyze" in form or "auto_draft" in form) and not settings.secret("OLLAMA_API_KEY"):
            raise ValueError("Add an Ollama key before enabling automatic analysis")
        store.set_setting("roles",roles)
        store.set_setting("auto_analyze","auto_analyze" in form)
        store.set_setting("auto_draft","auto_draft" in form)
        store.set_setting("fallbacks_enabled","fallbacks_enabled" in form)
        store.event(None,"model_settings_updated","Stage-specific Ollama model assignments updated")
        return redirect("/settings","Settings saved locally. The key is never returned to the browser.")

    @app.post("/settings/catalog",dependencies=[Depends(mutation)])
    def refresh_catalog():
        router.catalog()
        return redirect("/settings","Cloud catalog refreshed; live inference access still requires a successful model call")

    @app.post("/settings/clear-pause",dependencies=[Depends(mutation)])
    def clear_model_pause():
        store.set_setting("ollama_blocked",False)
        return redirect("/settings","Model pause cleared. Retry a failed job after resolving the provider issue.")

    @app.post("/settings/remove-key",dependencies=[Depends(mutation)])
    def remove_key():
        if settings.ollama_api_key:
            raise ValueError("The active key comes from the environment; remove OLLAMA_API_KEY there and restart the application")
        settings.save_secret("OLLAMA_API_KEY","")
        return redirect("/settings","Locally saved key removed")

    @app.get("/jobs")
    def jobs_page(request: Request):
        return render(request,"jobs.html",jobs=store.jobs(),events=store.events(limit=20))

    @app.post("/jobs/{job_id}/retry",dependencies=[Depends(mutation)])
    def retry_job(job_id: int):
        with store.connect() as conn:
            job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not job or job["status"] != "failed":
            raise ValueError("Only failed jobs can be retried")
        store.enqueue(job["kind"],job["listing_id"],json.loads(job["payload"]))
        return redirect("/jobs","A new retry job was queued; the original failure remains in the audit trail")

    @app.get("/connections")
    def connections_page(request: Request):
        return render(request,"connections.html",imap_configured=bool(settings.imap_host and settings.imap_user and settings.imap_password and settings.imap_senders),latest_screen=store.setting("latest_screen"),screen_events=[e for e in store.events(limit=50) if e["kind"]=="screen_interpreted"])

    @app.get("/android")
    def android_page(request: Request):
        config = load_config(store)
        identifier = store.setting("android_inspection", "")
        nodes = []
        if re.fullmatch(r"[0-9a-f]{32}", identifier):
            path = settings.data_dir / "screens" / f"{identifier}.xml"
            if path.exists():
                nodes = source_nodes(path.read_text(encoding="utf-8"), config.package)
        image_exists = bool(re.fullmatch(r"[0-9a-f]{32}", identifier) and (settings.data_dir / "screens" / f"{identifier}.png").exists())
        return render(request, "android.html", android_config=config, pilot=store.setting("android_pilot", {}), devices=store.setting("android_devices", {}), foreground=store.setting("android_foreground", {}), nodes=nodes, inspected_screen=identifier, phone_image_exists=image_exists, deliveries=DeviceLedger(store).deliveries(), selector_roles=sorted(SCAN_ROLES | SEND_ROLES | OPTIONAL_ROLES))

    @app.post("/android/devices", dependencies=[Depends(mutation)])
    def android_devices():
        config = load_config(store)
        devices = adb_devices()
        store.set_setting("android_devices", devices)
        selected = config.udid or (devices["devices"][0]["udid"] if len(devices["devices"]) == 1 else "")
        store.set_setting("android_foreground", {"udid": selected, **adb_foreground(selected)})
        return redirect("/android", "USB device check finished. Open Flatsforfriends before checking its foreground package/activity.")

    @app.post("/android/config", dependencies=[Depends(mutation)])
    async def android_save(request: Request):
        form = await request.form()
        values = load_config(store).model_dump()
        for name in ("udid", "package", "activity", "mode", "poll_seconds", "max_pages", "max_cards", "detail_swipes", "daily_sends", "refresh", "identity_attribute", "thread_matches"):
            if name in form:
                values[name] = str(form[name]).strip()
        values["enabled"] = "enabled" in form
        values["auto_followups"] = "auto_followups" in form
        values["selectors"] = {}
        for role in SCAN_ROLES | SEND_ROLES | OPTIONAL_ROLES:
            value = str(form.get(f"selector_{role}_value", "")).strip()
            if value:
                values["selectors"][role] = {"using": str(form.get(f"selector_{role}_using", "id")), "value": value}
        config = AndroidConfig.model_validate(values)
        if config.enabled and config.mode == "automatic":
            if not settings.secret("OLLAMA_API_KEY") or not store.profile().first_name.strip() or not store.profile().introduction.strip():
                raise ValueError("Connect Ollama and complete your truthful profile before enabling automatic enquiries")
        save_config(store, config, acknowledged="pilot_acknowledged" in form)
        if config.enabled:
            store.enqueue("android_poll")
        return redirect("/android", "Phone settings saved. Monitoring " + ("enabled" if config.enabled else "paused"))

    @app.post("/android/jobs/{kind}", dependencies=[Depends(mutation)])
    def android_job(kind: str):
        if kind not in {"capture", "pilot", "poll"}:
            raise ValueError("Unknown phone task")
        store.enqueue("android_" + kind)
        return redirect("/android", "Phone task queued. See Work & activity for progress or errors.")

    @app.post("/android/pause", dependencies=[Depends(mutation)])
    def android_pause():
        AndroidPipeline(settings, store).pause("Phone monitoring paused by owner")
        return redirect("/android", "Phone actions paused; other search sources can continue")

    @app.post("/android/messages/{message_id}/approve", dependencies=[Depends(mutation)])
    async def android_approve(request: Request, message_id: int):
        form = await request.form()
        if "acknowledged" not in form:
            raise ValueError("Approve the exact saved text and recipient before queuing a phone send")
        ledger = DeviceLedger(store)
        ledger.approve(message_id, int(str(form["version"])), str(form.get("body", "")))
        message = store.message(message_id)
        store.enqueue("android_send", message["listing_id"], {"message_id": message_id, "mode": "approved"})
        return redirect(f"/listings/{message['listing_id']}", "Exact phone send queued; the worker rechecks the live listing and conversation before sending")

    @app.post("/android/deliveries/{attempt_id}/reconcile", dependencies=[Depends(mutation)])
    async def android_reconcile(request: Request, attempt_id: int):
        form = await request.form()
        if "acknowledged" not in form:
            raise ValueError("Check the actual conversation on your phone before reconciling delivery")
        DeviceLedger(store).reconcile(attempt_id, str(form.get("outcome", "")), str(form.get("evidence", "")))
        return redirect("/android", "Delivery evidence recorded. A failed send requires a deliberate retry in Work & activity.")

    @app.post("/connections/probe",dependencies=[Depends(mutation)])
    def probe_connections():
        state,detail = DesktopInspector().status()
        store.connector("cua",state,detail)
        state,detail = AndroidInspector(settings.appium_url).status()
        store.connector("appium",state,detail)
        return redirect("/connections","Local connection checks finished. Sending has not been enabled.")

    @app.post("/connections/imap",dependencies=[Depends(mutation)])
    def imap_job():
        store.enqueue("sync_imap")
        return redirect("/connections","Read-only IMAP sync queued")

    @app.post("/connections/capture",dependencies=[Depends(mutation)])
    async def capture(request: Request):
        form = await request.form()
        result = AndroidInspector(settings.appium_url).capture(str(form.get("session_id","")),settings.data_dir/"screens")
        store.set_setting("latest_screen",result["id"])
        store.event(None,"android_snapshot","Captured an existing Appium session; no input actions were performed")
        return redirect("/connections","Android screenshot and accessibility source captured locally")

    @app.post("/connections/screen",dependencies=[Depends(mutation)])
    async def inspect_screen(request: Request):
        form = await request.form()
        upload = form.get("screen_file")
        identifier = store.setting("latest_screen")
        if upload and getattr(upload,"filename",""):
            raw = await upload.read(5_000_001)
            if not raw.startswith(b"\x89PNG\r\n\x1a\n") or len(raw)>5_000_000:
                raise ValueError("Choose a PNG screenshot smaller than 5 MB")
            folder = settings.data_dir/"screens"
            folder.mkdir(exist_ok=True)
            identifier = uuid.uuid4().hex
            (folder/f"{identifier}.png").write_bytes(raw)
            store.set_setting("latest_screen",identifier)
        if not identifier:
            raise ValueError("Capture or upload a screenshot first")
        if "cloud_consent" not in form:
            raise ValueError("Confirm sending this app screenshot to Ollama Cloud for interpretation")
        store.enqueue("screen",payload={"screen_id":identifier,"goal":str(form.get("goal","Inspect this rental app screen"))[:1000]})
        return redirect("/connections","Vision interpretation queued. It proposes an action without executing it.")

    @app.get("/screens/{identifier}.png")
    def screenshot(identifier: str):
        if not re.fullmatch(r"[0-9a-f]{32}",identifier):
            raise KeyError("Screenshot not found")
        path = settings.data_dir/"screens"/f"{identifier}.png"
        if not path.exists():
            raise KeyError("Screenshot not found")
        return FileResponse(path,media_type="image/png")

    @app.get("/export/listings.csv")
    def export_csv():
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["id","title","source","stage","eligibility","total_monthly_eur","distance_km","cycling_minutes","transit_minutes","url"])
        def cell(value):
            text = str(value if value is not None else "")
            return "'"+text if text.startswith(("=","+","-","@","\t","\r")) else text
        for item in store.listings():
            d = item["data"]
            writer.writerow([cell(v) for v in [item["id"],d["title"],d["source"],item["stage"],item["match"]["status"],item["match"]["monthly_total"],d["distance_km"],d["cycling_minutes"],d["transit_minutes"],d["url"]]])
        return Response(output.getvalue(),media_type="text/csv",headers={"Content-Disposition":'attachment; filename="homeward-listings.csv"'})

    @app.get("/export/viewings.ics")
    def export_calendar():
        def escape(value):
            return value.replace("\\","\\\\").replace("\n","\\n").replace("\r","").replace(",","\\,").replace(";","\\;")
        lines = ["BEGIN:VCALENDAR","VERSION:2.0","PRODID:-//Homeward//Munich Flat Agent//EN","CALSCALE:GREGORIAN"]
        for v in store.viewings():
            if v["status"]!="confirmed":
                continue
            start = datetime.fromisoformat(v["starts_at"]).astimezone(timezone.utc)
            end = start+timedelta(minutes=v["duration_minutes"])
            lines += ["BEGIN:VEVENT",f"UID:homeward-viewing-{v['id']}@local",f"DTSTAMP:{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",f"DTSTART:{start.strftime('%Y%m%dT%H%M%SZ')}",f"DTEND:{end.strftime('%Y%m%dT%H%M%SZ')}","SUMMARY:Flat viewing",f"LOCATION:{escape(v['location'])}","END:VEVENT"]
        lines.append("END:VCALENDAR")
        return Response("\r\n".join(lines)+"\r\n",media_type="text/calendar",headers={"Content-Disposition":'attachment; filename="homeward-viewings.ics"'})

    @app.get("/api/meta")
    def meta():
        config = load_config(store)
        pilot = store.setting("android_pilot", {})
        enabled = config.enabled and config.mode == "automatic" and pilot.get("fingerprint") == config.fingerprint() and pilot.get("conversation_verified", False)
        return {"csrf_token":settings.csrf_token(),"live_sending_enabled":enabled,"key_configured":bool(settings.secret("OLLAMA_API_KEY"))}

    @app.get("/api/listings")
    def list_api():
        return store.listings()

    @app.post("/api/listings",dependencies=[Depends(mutation)])
    def add_api(data: ListingInput):
        identifier,created = store.add_listing(data)
        return {"id":identifier,"created":created,"listing":store.listing(identifier)}

    @app.get("/api/jobs")
    def jobs_api():
        return store.jobs()

    @app.get("/api/profile")
    def profile_api():
        return store.profile().model_dump(mode="json")

    @app.get("/api/status")
    def status_api():
        return {"connectors":store.connectors(),"profile_paused":store.profile().paused,"search_complete":store.profile().search_complete,"live_sending_enabled":False}

    return app
