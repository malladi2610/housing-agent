from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Stage(StrEnum):
    NEW = "new"
    MATCHED = "matched"
    NEEDS_REVIEW = "needs_review"
    DRAFT_READY = "draft_ready"
    MESSAGE_SENT = "message_sent"
    RESPONSE_RECEIVED = "response_received"
    VIEWING_PROPOSED = "viewing_proposed"
    VIEWING_CONFIRMED = "viewing_confirmed"
    VIEWED = "viewed"
    SECURED = "secured"
    REJECTED = "rejected"
    CLOSED = "closed"
    ARCHIVED = "archived"


CLOSED_STAGES = {Stage.SECURED, Stage.REJECTED, Stage.CLOSED, Stage.ARCHIVED}


class Profile(StrictModel):
    revision: int = 1
    first_name: str = Field(default="", max_length=80)
    introduction: str = Field(default="", max_length=2500)
    language: Literal["German", "English"] = "German"
    availability: str = Field(default="", max_length=1200)
    writing_example: str = Field(default="", max_length=2000)
    max_monthly_total: float = Field(default=850, gt=0, le=100000)
    target_move_in: date = date(2026, 11, 15)
    latest_move_in: date = date(2027, 1, 1)
    max_distance_km: float = Field(default=6, gt=0, le=100)
    office_address: str = "MTZ, Agnes-Pockels-Bogen 1, 80992 München"
    home_types: list[Literal["studio", "shared"]] = Field(default_factory=lambda: ["studio", "shared"])
    followup_days: int = Field(default=3, ge=1, le=30)
    contact_start_hour: int = Field(default=9, ge=0, le=23)
    contact_end_hour: int = Field(default=20, ge=1, le=24)
    daily_model_calls: int = Field(default=100, ge=1, le=5000)
    paused: bool = False
    search_complete: bool = False

    @model_validator(mode="after")
    def check_window(self):
        if self.target_move_in > self.latest_move_in:
            raise ValueError("Target move-in must not be after the latest move-in")
        if self.contact_start_hour >= self.contact_end_hour:
            raise ValueError("Contact hours must start before they end")
        if not self.home_types:
            raise ValueError("Select at least one accommodation type")
        return self


class ListingInput(StrictModel):
    source: Literal["manual", "wg-gesucht", "flatsforfriendz", "immoscout24", "immowelt"] = "manual"
    external_id: str = Field(default="", max_length=250)
    url: HttpUrl | None = None
    title: str = Field(min_length=1, max_length=250)
    description: str = Field(default="", max_length=50000)
    area: str = Field(default="", max_length=200)
    address: str = Field(default="", max_length=400)
    contact_name: str = Field(default="", max_length=120)
    home_type: Literal["studio", "shared", "unknown"] = "unknown"
    warm_rent: float | None = Field(default=None, ge=0, le=100000)
    electricity: float | None = Field(default=None, ge=0, le=100000)
    internet: float | None = Field(default=None, ge=0, le=100000)
    other_monthly: float | None = Field(default=None, ge=0, le=100000)
    all_in_rent: float | None = Field(default=None, ge=0, le=100000)
    all_costs_confirmed: bool = False
    cost_evidence: str = Field(default="", max_length=2000)
    available_from: date | None = None
    available_until: date | None = None
    move_in_confirmed: bool = False
    availability_evidence: str = Field(default="", max_length=2000)
    distance_km: float | None = Field(default=None, ge=0, le=1000)
    distance_basis: Literal["route", "straight_line", "unknown"] = "unknown"
    cycling_minutes: int | None = Field(default=None, ge=0, le=600)
    transit_minutes: int | None = Field(default=None, ge=0, le=600)
    commute_evidence: str = Field(default="", max_length=1000)
    request_notes: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def check_dates(self):
        if self.available_from and self.available_until and self.available_until < self.available_from:
            raise ValueError("Lease end precedes availability")
        return self

    def monthly_total(self) -> float | None:
        if self.all_in_rent is not None:
            return self.all_in_rent
        components = (self.warm_rent, self.electricity, self.internet, self.other_monthly)
        return round(sum(components), 2) if all(v is not None for v in components) else None


class MatchResult(StrictModel):
    status: Literal["match", "needs_review", "excluded"]
    reasons: list[str]
    monthly_total: float | None
    profile_revision: int


class Extraction(StrictModel):
    """Fields inferred from listing text; source identifiers are immutable."""
    home_type: Literal["studio", "shared", "unknown"] = "unknown"
    area: str = Field(default="", max_length=200)
    address: str = Field(default="", max_length=400)
    contact_name: str = Field(default="", max_length=120)
    warm_rent: float | None = Field(default=None, ge=0, le=100000)
    electricity: float | None = Field(default=None, ge=0, le=100000)
    internet: float | None = Field(default=None, ge=0, le=100000)
    other_monthly: float | None = Field(default=None, ge=0, le=100000)
    all_in_rent: float | None = Field(default=None, ge=0, le=100000)
    all_costs_confirmed: bool = False
    cost_evidence: str = Field(default="", max_length=2000)
    available_from: date | None = None
    available_until: date | None = None
    move_in_confirmed: bool = False
    availability_evidence: str = Field(default="", max_length=2000)
    request_notes: str = Field(default="", max_length=2000)


class DraftOutput(StrictModel):
    text: str = Field(min_length=10, max_length=2200)
    used_profile_fields: list[Literal["first_name", "introduction", "availability", "target_move_in", "latest_move_in"]]
    listing_detail: str = Field(min_length=1, max_length=500)
    needs_user_input: list[str] = Field(default_factory=list, max_length=20)


class ReplyOutput(StrictModel):
    intent: Literal["question", "viewing_invitation", "rejection", "document_request", "unclear", "other"]
    summary: str = Field(max_length=1200)
    suggested_reply: str = Field(default="", max_length=2200)
    needs_user_input: list[str] = Field(default_factory=list, max_length=20)


class ScreenOutput(StrictModel):
    summary: str = Field(max_length=2000)
    target_description: str = Field(max_length=1000)
    proposed_action: Literal["click", "type", "scroll", "wait", "stop"]
    reason: str = Field(max_length=1000)


class ViewingInput(StrictModel):
    starts_at: datetime
    duration_minutes: int = Field(default=30, ge=5, le=180)
    location: str = Field(min_length=1, max_length=500)
    confirmed: bool = False
    confirmation_evidence: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def confirmation(self):
        if self.starts_at.tzinfo is None:
            raise ValueError("Viewing requires an explicit timezone")
        if self.confirmed and not self.confirmation_evidence.strip():
            raise ValueError("A confirmed viewing needs evidence of the other party's acceptance")
        return self


DEFAULT_ROLES = {
    "extract_and_classify": {"model": "glm-5.3-flash", "fallback_model": "glm-5.3", "required_capabilities": ["completion"]},
    "draft_message": {"model": "kimi-k3", "fallback_model": "glm-5.3-flash", "required_capabilities": ["completion"]},
    "interpret_complex_reply": {"model": "glm-5.3", "fallback_model": "kimi-k3", "required_capabilities": ["completion"]},
    "interpret_screen": {"model": "kimi-k3", "fallback_model": "glm-5.3-flash", "required_capabilities": ["completion", "vision"]},
}
