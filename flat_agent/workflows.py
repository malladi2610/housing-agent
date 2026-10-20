from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.checkpoint.sqlite import SqliteSaver

from .db import Store
from .matching import evaluate
from .models import CLOSED_STAGES, DraftOutput, Extraction, ListingInput, ReplyOutput
from .android_store import body_hash


class WorkflowState(TypedDict, total=False):
    listing_id: int
    listing_version: int
    profile_revision: int
    extraction: dict
    match: dict
    operation: str
    result: str


class Workflows:
    def __init__(self, store: Store, router, checkpoint_path):
        self.store, self.router, self.checkpoint_path = store, router, checkpoint_path

    def analyze(self, listing_id: int, job_id: int):
        graph = StateGraph(WorkflowState)
        graph.add_node("extract", self._extract)
        graph.add_node("validate_and_match", self._apply_extraction)
        graph.add_edge(START,"extract")
        graph.add_edge("extract","validate_and_match")
        graph.add_edge("validate_and_match",END)
        with SqliteSaver.from_conn_string(str(self.checkpoint_path)) as saver:
            app = graph.compile(checkpointer=saver)
            config = {"configurable":{"thread_id":f"listing-{listing_id}:job-{job_id}"}}
            saved = app.get_state(config)
            if saved.next:
                return app.invoke(None,config)
            if saved.values.get("result") == "Analysis complete":
                return saved.values
            return app.invoke({"listing_id":listing_id,"operation":"analyze"},config)

    def _extract(self, state: WorkflowState):
        listing = self.store.listing(state["listing_id"])
        profile = self.store.profile()
        if listing["stage"] in CLOSED_STAGES:
            raise ValueError("Listing is closed")
        result, model = self.router.ask("extract_and_classify",Extraction,
            "Extract facts from the listing only. Dates must include a year; if the year is not explicit, keep the date null. Do not infer commute distance or travel times. Set all_costs_confirmed only if the source explicitly includes every recurring cost. Copy exact short source quotes into cost_evidence and availability_evidence. Set move_in_confirmed only when an explicit source date is compatible with the search window; older availability that may require earlier rent is unconfirmed.",
            {"listing":listing["data"],"search_window":{"target":str(profile.target_move_in),"latest":str(profile.latest_move_in)}})
        return {"extraction":result.model_dump(mode="json"),"listing_version":listing["version"],"profile_revision":profile.revision}

    def _apply_extraction(self, state: WorkflowState):
        listing = self.store.listing(state["listing_id"])
        if listing["version"] != state["listing_version"] or self.store.profile().revision != state["profile_revision"]:
            raise ValueError("Facts changed while extracting; run analysis again")
        original = listing["data"]
        extracted = dict(state["extraction"])
        source = (original["title"] + "\n" + original["description"]).casefold()
        for evidence_key in ("cost_evidence","availability_evidence"):
            evidence = extracted.get(evidence_key,"")
            if evidence and evidence.casefold() not in source:
                extracted[evidence_key] = ""
        if not extracted.get("cost_evidence"):
            extracted["all_costs_confirmed"] = False
        if not extracted.get("availability_evidence"):
            extracted["move_in_confirmed"] = False
        # A valid schema and a real quote do not prove a model's numeric claims.
        # Nonzero extracted costs must also occur in the quoted cost evidence.
        quoted_numbers = set()
        for token in re.findall(r"(?<![\w])\d+(?:[.,]\d+)*(?![\w])",extracted.get("cost_evidence","")):
            try:
                if "," in token:
                    normalized = token.replace(".","").replace(",",".")
                elif token.count(".")>1 or ("." in token and len(token.rsplit(".",1)[1])==3):
                    normalized = token.replace(".","")
                else:
                    normalized = token
                quoted_numbers.add(Decimal(normalized))
            except InvalidOperation:
                pass
        for field in ("warm_rent","electricity","internet","other_monthly","all_in_rent"):
            value = extracted.get(field)
            if value is not None and (value != 0 or field in {"all_in_rent","warm_rent"}) and Decimal(str(value)) not in quoted_numbers:
                extracted[field] = None
                extracted["all_costs_confirmed"] = False
        for field in ("available_from","available_until"):
            value = extracted.get(field)
            if value:
                from datetime import date
                parsed = date.fromisoformat(value)
                evidence = extracted.get("availability_evidence","")
                candidates = {parsed.isoformat(),parsed.strftime("%d.%m.%Y"),parsed.strftime("%d/%m/%Y"),f"{parsed.day}.{parsed.month}.{parsed.year}"}
                if not any(candidate in evidence for candidate in candidates):
                    extracted[field] = None
                    extracted["move_in_confirmed"] = False
        # Never overwrite facts the owner already entered or confirmed.
        human_fields = set(self.store.setting(f"manual_fields:{listing['id']}",[]))
        for key, value in extracted.items():
            if key not in human_fields and value is not None and value != "":
                original[key] = value
        validated = ListingInput.model_validate(original)
        self.store.update_listing(listing["id"],validated,listing["version"])
        match = evaluate(validated,self.store.profile())
        self.store.event(listing["id"],"analysis_complete","Text extraction validated; matching rules applied")
        return {"match":match.model_dump(mode="json"),"result":"Analysis complete"}

    def draft(self, listing_id: int, kind="initial", in_reply_to=None):
        listing = self.store.listing(listing_id)
        profile = self.store.profile()
        if not profile.first_name.strip() or not profile.introduction.strip():
            raise ValueError("Add your first name and truthful introduction in Search profile before drafting")
        if listing["stage"] in CLOSED_STAGES:
            raise ValueError("Listing is closed")
        if listing["match"]["status"] == "excluded":
            raise ValueError("This listing fails a hard requirement; update confirmed facts before drafting")
        if listing["match"]["status"] != "match":
            task_note = "The listing has unconfirmed facts. Ask about the missing costs/date/distance where appropriate; do not claim it is a confirmed match."
        else:
            task_note = "The confirmed facts meet the search requirements."
        messages = self.store.messages(listing_id)
        result, model = self.router.ask("draft_message",DraftOutput,
            f"Write a {kind} rental enquiry in {profile.language}, around 60–100 words. Use a warm simple greeting, one specific real listing detail, a brief relevant approved introduction and a clear viewing request. Answer application questions when supported. Never invent personal facts, hobbies, salary or fluency. Match the user's writing example when present. Do not say you are human or claim a viewing is confirmed. Do not output placeholders. {task_note}",
            {"approved_profile":profile.model_dump(mode="json"),"listing":listing["data"],"unconfirmed":listing["match"]["reasons"],"conversation":[{"direction":m["direction"],"body":m["body"],"status":m["status"]} for m in messages[-12:]]})
        if result.needs_user_input:
            self.store.event(listing_id,"draft_needs_input","; ".join(result.needs_user_input))
        if re.search(r"\[[^\]]+\]|\{[^}]+\}",result.text):
            raise ValueError("Draft contains unresolved placeholders; add missing profile details")
        if in_reply_to is None:
            inbound = [m for m in messages if m["direction"] == "inbound"]
            in_reply_to = inbound[-1]["id"] if inbound else None
        identifier = self.store.add_draft(listing_id,result.text,model,profile.revision,listing["version"],kind,in_reply_to)
        source = (listing["data"]["title"] + "\n" + listing["data"]["description"]).casefold()
        self.store.set_setting(f"draft_review:{identifier}", {"grounded": result.listing_detail.casefold() in source, "needs_user_input": result.needs_user_input, "body_hash": body_hash(result.text)})
        return identifier

    def reply(self, listing_id: int, message_id: int):
        profile = self.store.profile()
        listing = self.store.listing(listing_id)
        message = self.store.message(message_id)
        if message["listing_id"] != listing_id or message["direction"] != "inbound":
            raise ValueError("Reply does not belong to this conversation")
        result, model = self.router.ask("interpret_complex_reply",ReplyOutput,
            f"Classify the incoming reply and suggest a brief answer in {profile.language} using approved facts. An invitation or proposed time is not an accepted appointment. Never agree to payments/contracts or disclose documents. Document requests and missing personal facts require user input. Do not interpret notification previews as complete conversations. If the introduction is empty, ask the owner to complete their profile and leave suggested_reply empty.",
            {"approved_profile":profile.model_dump(mode="json"),"listing":listing["data"],"incoming_reply":message["body"]})
        self.store.event(listing_id,"reply_interpreted",json.dumps(result.model_dump(mode="json"),ensure_ascii=False))
        if result.suggested_reply and not result.needs_user_input and profile.first_name and profile.introduction:
            self.store.add_draft(listing_id,result.suggested_reply,model,profile.revision,listing["version"],"reply",message_id)
        return result
