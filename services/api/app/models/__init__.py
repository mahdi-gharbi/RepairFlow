"""Relational model. PostgreSQL is the source of truth for every business decision."""
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Computed, Date, DateTime, ForeignKey, Index, Integer, String, Text,
    UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

INCIDENT_STATES = ("received", "enriching", "needs_review", "approved", "dispatching", "completed", "blocked", "rejected")
ACTION_STATES = ("pending", "offered", "executing", "succeeded", "retry_wait", "uncertain", "dead", "cancelled")


def _in(col: str, values: tuple[str, ...]) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


class Base(DeclarativeBase):
    pass


class Team(Base):
    __tablename__ = "teams"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    # Only recipient source for notifications. Never taken from a report or a model output.
    notify_email: Mapped[str] = mapped_column(String(320))


class Equipment(Base):
    __tablename__ = "equipment"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    model: Mapped[str] = mapped_column(String(64))
    display_name: Mapped[str] = mapped_column(String(200))
    site: Mapped[str] = mapped_column(String(200))
    telemetry_source: Mapped[str] = mapped_column(String(64))
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.id"))


class Incident(Base):
    __tablename__ = "incidents"
    __table_args__ = (CheckConstraint(_in("status", INCIDENT_STATES), name="ck_incident_status"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    equipment_id: Mapped[str] = mapped_column(ForeignKey("equipment.id"))
    status: Mapped[str] = mapped_column(String(32))
    current_revision: Mapped[int] = mapped_column(Integer)
    correlation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InboxEvent(Base):
    __tablename__ = "inbox_events"
    __table_args__ = (UniqueConstraint("source_scope", "event_id", name="uq_inbox_scope_event"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_scope: Mapped[str] = mapped_column(String(64))
    event_id: Mapped[str] = mapped_column(String(128))
    payload_hash: Mapped[str] = mapped_column(String(64))
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incidents.id"))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IncidentRevision(Base):
    """Immutable snapshot of business data. A new correction or evidence = a new row."""
    __tablename__ = "incident_revisions"
    __table_args__ = (UniqueConstraint("incident_id", "revision", name="uq_revision"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incidents.id"))
    revision: Mapped[int] = mapped_column(Integer)
    data: Mapped[dict] = mapped_column(JSONB)
    reason: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EvidenceSnapshot(Base):
    __tablename__ = "evidence_snapshots"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incidents.id"))
    kind: Mapped[str] = mapped_column(String(32))  # message | telemetry | manual
    source_id: Mapped[str] = mapped_column(String(128))
    source_version: Mapped[str] = mapped_column(String(128))
    content_hash: Mapped[str] = mapped_column(String(64))
    content: Mapped[dict] = mapped_column(JSONB)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TriageRun(Base):
    __tablename__ = "triage_runs"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incidents.id"))
    incident_revision: Mapped[int] = mapped_column(Integer)
    engine: Mapped[str] = mapped_column(String(32))  # rules | llm | retrieval_llm
    model: Mapped[str | None] = mapped_column(String(128))
    model_digest: Mapped[str | None] = mapped_column(String(128))
    prompt_version: Mapped[str | None] = mapped_column(String(64))
    input_hash: Mapped[str] = mapped_column(String(64))
    raw_output: Mapped[str | None] = mapped_column(Text)
    proposal: Mapped[dict | None] = mapped_column(JSONB)
    valid: Mapped[bool] = mapped_column(Boolean)
    errors: Mapped[list] = mapped_column(JSONB, default=list)
    duration_ms: Mapped[int] = mapped_column(Integer)
    # Exact context sent to the model and inference parameters / response metadata (no secrets).
    context: Mapped[dict | None] = mapped_column(JSONB)
    params: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RetrievalChunk(Base):
    """One manual section. Lexical index = PostgreSQL full-text (english config) on section + text."""
    __tablename__ = "retrieval_chunks"
    __table_args__ = (
        UniqueConstraint("source_id", "version", "section", name="uq_chunk"),
        Index("ix_chunks_tsv", "tsv", postgresql_using="gin"),
    )
    id: Mapped[str] = mapped_column(String(160), primary_key=True)  # source_id@version#n
    source_id: Mapped[str] = mapped_column(String(64))
    version: Mapped[str] = mapped_column(String(32))
    published_at: Mapped[datetime] = mapped_column(Date)
    title: Mapped[str] = mapped_column(String(200))
    section: Mapped[str] = mapped_column(String(200))
    span_start: Mapped[int] = mapped_column(Integer)
    span_end: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    models: Mapped[list[str]] = mapped_column(ARRAY(String(64)))
    superseded: Mapped[bool] = mapped_column(Boolean, default=False)
    content_hash: Mapped[str] = mapped_column(String(64))
    tsv = mapped_column(TSVECTOR, Computed("to_tsvector('english', section || ' ' || text)", persisted=True))


class Approval(Base):
    __tablename__ = "approvals"
    __table_args__ = (UniqueConstraint("incident_id", "incident_revision", name="uq_approval_revision"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incidents.id"))
    incident_revision: Mapped[int] = mapped_column(Integer)
    reviewer: Mapped[str] = mapped_column(String(64))
    plan: Mapped[dict] = mapped_column(JSONB)
    plan_digest: Mapped[str] = mapped_column(String(64))
    policy_version: Mapped[str] = mapped_column(String(32))
    evidence_versions: Mapped[list] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Action(Base):
    __tablename__ = "actions"
    __table_args__ = (CheckConstraint(_in("status", ACTION_STATES), name="ck_action_status"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incidents.id"))
    approval_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("approvals.id"))
    type: Mapped[str] = mapped_column(String(32))  # create_ticket | notify_team
    args: Mapped[dict] = mapped_column(JSONB)  # frozen at approval time
    action_key: Mapped[str] = mapped_column(String(200), unique=True)
    depends_on: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("actions.id"))
    status: Mapped[str] = mapped_column(String(32))
    provider_reference: Mapped[str | None] = mapped_column(String(200))
    last_error: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Outbox(Base):
    __tablename__ = "outbox"
    __table_args__ = (
        CheckConstraint("attempts >= 0", name="ck_outbox_attempts"),
        CheckConstraint("fencing_token >= 0", name="ck_outbox_fencing"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    action_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("actions.id"), unique=True)
    # NULL = not eligible yet (e.g. waiting for a dependency).
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    offers: Mapped[int] = mapped_column(Integer, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fencing_token: Mapped[int] = mapped_column(BigInteger, default=0)
    dispatch_token_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditEvent(Base):
    """Append-only at application level (no UPDATE/DELETE code path). Not tamper-proof against a DB admin."""
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    incident_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    action_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    correlation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    event: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(64))
    from_state: Mapped[str | None] = mapped_column(String(32))
    to_state: Mapped[str | None] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
