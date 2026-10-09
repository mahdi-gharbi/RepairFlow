"""Wire contracts. Model outputs and reports are untrusted data and must pass these schemas."""
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ID = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IncidentReport(Strict):
    event_id: str = Field(pattern=ID)
    equipment_id: str = Field(pattern=ID)
    reported_at: datetime
    language: Literal["fr", "en"]
    description: str = Field(min_length=3, max_length=2000)
    operator_reference: str | None = Field(default=None, pattern=ID)

    @field_validator("reported_at")
    @classmethod
    def utc_only(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() != timedelta(0):
            raise ValueError("reported_at must be an explicit UTC timestamp (Z or +00:00)")
        return v


class IntakeRequest(Strict):
    source_scope: str = Field(pattern=ID)
    report: IncidentReport


class TelemetryEvidence(Strict):
    """Telemetry as fetched by the intake workflow. `available=False` records a missing source."""
    source_id: str = Field(pattern=ID)
    available: bool
    observed_at: datetime | None = None
    values: dict[str, float | int | str | bool | None] = Field(default_factory=dict, max_length=50)
    error: str | None = Field(default=None, max_length=300)


Category = Literal["battery", "network", "mechanical", "sensor", "other"]


class Fact(Strict):
    claim: str = Field(max_length=500)
    evidence_refs: list[str] = Field(min_length=1, max_length=10)


class Contradiction(Strict):
    claim_a: str = Field(max_length=500)
    claim_b: str = Field(max_length=500)
    evidence_refs: list[str] = Field(min_length=1, max_length=10)


class DraftTicket(Strict):
    title: str = Field(min_length=3, max_length=160)
    description: str = Field(max_length=4000)


class TriageProposal(Strict):
    incident_revision: int
    category: Category
    summary: str = Field(max_length=1000)
    extracted_facts: list[Fact] = Field(max_length=20)
    missing_information: list[str] = Field(max_length=10)
    contradictions: list[Contradiction] = Field(max_length=10)
    suggested_next_step: Literal["clarify", "review", "prepare_ticket"]
    draft_ticket: DraftTicket
    evidence_manifest_id: str


class Correction(Strict):
    expected_revision: int
    note: str = Field(max_length=1000)
    category: Category | None = None
    ticket_title: str | None = Field(default=None, min_length=3, max_length=160)
    ticket_description: str | None = Field(default=None, max_length=4000)


class ApproveRequest(Strict):
    expected_revision: int
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class RejectRequest(Strict):
    expected_revision: int
    reason: str = Field(max_length=500)


class BeginRequest(Strict):
    fencing_token: int
    dispatch_token: str = Field(max_length=200)


Outcome = Literal["succeeded", "retryable", "rate_limited", "permanent", "ambiguous"]


class AckRequest(Strict):
    fencing_token: int
    dispatch_token: str = Field(max_length=200)
    outcome: Outcome
    provider_reference: str | None = Field(default=None, max_length=200)
    http_status: int | None = None
    retry_after_s: float | None = Field(default=None, ge=0, le=86400)
    detail: str | None = Field(default=None, max_length=500)


class ResolveRequest(Strict):
    """Human decision on an uncertain/dead action."""
    decision: Literal["mark_succeeded", "retry", "give_up"]
    provider_reference: str | None = Field(default=None, max_length=200)
    note: str = Field(max_length=500)
