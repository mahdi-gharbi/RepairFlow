"""Incident lifecycle: atomic intake, immutable revisions, triage, corrections and versioned approval."""
import time
import uuid
from datetime import datetime

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import audit
from app.canonical import sha256_hex
from app.config import get_settings
from app.contracts import ApproveRequest, Correction, IntakeRequest, RejectRequest, TelemetryEvidence, TriageProposal
from app.errors import Conflict, Invalid, NotFound
from app.models import (
    Action, Approval, Equipment, EvidenceSnapshot, InboxEvent, Incident, IncidentRevision, Outbox, Team, TriageRun,
)
from app.policy import POLICY_VERSION, build_plan, plan_digest
from app.transitions import ACTION_NOT_STARTED, check_action, check_incident
from app.triage import rules


def now(db: Session) -> datetime:
    """PostgreSQL is the only clock (transaction start time): leases stay consistent across hosts."""
    return db.execute(text("SELECT now()")).scalar_one()


def set_incident_status(db: Session, incident: Incident, target: str, actor: str, event: str, **payload) -> None:
    previous = incident.status
    check_incident(previous, target)
    incident.status = target
    incident.updated_at = now(db)
    audit.record(db, event, actor, incident_id=incident.id, correlation_id=incident.correlation_id,
                 from_state=previous, to_state=target, payload=payload)


def set_action_status(db: Session, action: Action, target: str, actor: str, event: str, correlation_id=None, **payload) -> None:
    previous = action.status
    check_action(previous, target)
    action.status = target
    action.updated_at = now(db)
    audit.record(db, event, actor, incident_id=action.incident_id, action_id=action.id, correlation_id=correlation_id,
                 from_state=previous, to_state=target, payload=payload)


def lock_incident(db: Session, incident_id: uuid.UUID) -> Incident:
    incident = db.execute(select(Incident).where(Incident.id == incident_id).with_for_update()).scalar_one_or_none()
    if incident is None:
        raise NotFound("incident_not_found", str(incident_id))
    return incident


def current_revision(db: Session, incident: Incident) -> IncidentRevision:
    return db.execute(select(IncidentRevision).where(
        IncidentRevision.incident_id == incident.id, IncidentRevision.revision == incident.current_revision)).scalar_one()


def _new_revision(db: Session, incident: Incident, data: dict, reason: str, actor: str) -> IncidentRevision:
    incident.current_revision += 1
    rev = IncidentRevision(incident_id=incident.id, revision=incident.current_revision, data=data, reason=reason, actor=actor)
    db.add(rev)
    audit.record(db, "revision_created", actor, incident_id=incident.id, correlation_id=incident.correlation_id,
                 payload={"revision": incident.current_revision, "reason": reason})
    return rev


# --- intake -------------------------------------------------------------------------------------

def intake(db: Session, req: IntakeRequest, actor: str) -> tuple[int, Incident]:
    """201 new incident, 200 replay of the same event, 409 same event_id with a different payload.

    Atomicity comes from INSERT .. ON CONFLICT DO NOTHING on UNIQUE(source_scope, event_id): a concurrent
    duplicate blocks on the first transaction's uncommitted row, then sees it once committed."""
    report = req.report
    payload_hash = sha256_hex(report.model_dump(mode="json"))
    if db.get(Equipment, report.equipment_id) is None:
        raise Invalid("unknown_equipment", report.equipment_id)

    incident = Incident(id=uuid.uuid4(), equipment_id=report.equipment_id, status="received", current_revision=1,
                        correlation_id=uuid.uuid4())
    db.add(incident)
    db.flush()
    msg = EvidenceSnapshot(
        id=f"msg-{uuid.uuid4().hex[:12]}", incident_id=incident.id, kind="message",
        source_id=f"{req.source_scope}:{report.event_id}", source_version=payload_hash[:16], content_hash=payload_hash,
        content={"description": report.description, "language": report.language}, observed_at=report.reported_at)
    db.add(msg)
    db.add(IncidentRevision(incident_id=incident.id, revision=1, reason="intake", actor=actor, data={
        "description": report.description, "language": report.language,
        "reported_at": report.reported_at.isoformat(), "operator_reference": report.operator_reference,
        "evidence_ids": [msg.id], "overrides": {}, "notes": [],
    }))
    inserted = db.execute(
        pg_insert(InboxEvent)
        .values(source_scope=req.source_scope, event_id=report.event_id, payload_hash=payload_hash, incident_id=incident.id)
        .on_conflict_do_nothing(constraint="uq_inbox_scope_event")
        .returning(InboxEvent.id)
    ).scalar_one_or_none()

    if inserted is None:
        db.rollback()
        existing = db.execute(select(InboxEvent).where(
            InboxEvent.source_scope == req.source_scope, InboxEvent.event_id == report.event_id)).scalar_one()
        if existing.payload_hash != payload_hash:
            audit.record(db, "intake_conflict", actor, incident_id=existing.incident_id,
                         payload={"event_id": report.event_id, "source_scope": req.source_scope})
            db.commit()
            raise Conflict("event_payload_mismatch", "event_id already received with a different payload",
                           incident_id=str(existing.incident_id))
        audit.record(db, "intake_replayed", actor, incident_id=existing.incident_id, payload={"event_id": report.event_id})
        db.commit()
        return 200, db.get(Incident, existing.incident_id)

    audit.record(db, "incident_received", actor, incident_id=incident.id, correlation_id=incident.correlation_id,
                 to_state="received", payload={"event_id": report.event_id, "source_scope": req.source_scope,
                                               "equipment_id": report.equipment_id})
    db.commit()
    return 201, incident


# --- evidence -----------------------------------------------------------------------------------

def invalidate_approval(db: Session, incident: Incident, actor: str, reason: str) -> None:
    """A new revision supersedes the approved plan. Not-started actions are cancelled. Started ones
    cannot be recalled (an email may already be sent): record it and block the incident."""
    # Lock order everywhere: incident -> outbox -> action (the worker locks outbox -> action).
    ids = select(Action.id).where(Action.incident_id == incident.id).scalar_subquery()
    outbox = {o.action_id: o for o in db.execute(
        select(Outbox).where(Outbox.action_id.in_(ids)).order_by(Outbox.id).with_for_update()).scalars()}
    actions = db.execute(select(Action).where(Action.incident_id == incident.id, Action.status != "cancelled")
                         .order_by(Action.id).with_for_update()).scalars().all()
    started = [a for a in actions if a.status not in ACTION_NOT_STARTED]
    for a in actions:
        if a.status in ACTION_NOT_STARTED:
            set_action_status(db, a, "cancelled", actor, "action_cancelled_superseded", incident.correlation_id, reason=reason)
            ob = outbox[a.id]
            ob.available_at, ob.lease_owner, ob.lease_until, ob.dispatch_token_hash = None, None, None, None
    if started:
        set_incident_status(db, incident, "blocked", actor, "approval_superseded_after_start", reason=reason,
                            started_actions=[{"id": str(a.id), "type": a.type, "status": a.status} for a in started])
    else:
        set_incident_status(db, incident, "needs_review", actor, "approval_invalidated", reason=reason)


def add_telemetry(db: Session, incident_id: uuid.UUID, tel: TelemetryEvidence, actor: str) -> Incident:
    incident = lock_incident(db, incident_id)
    if incident.status in ("completed", "rejected"):
        raise Conflict("incident_closed", incident.status)
    content = tel.model_dump(mode="json")
    content_hash = sha256_hex(content)
    snap = EvidenceSnapshot(
        id=f"tel-{uuid.uuid4().hex[:12]}", incident_id=incident.id, kind="telemetry", source_id=tel.source_id,
        source_version=tel.observed_at.isoformat() if tel.observed_at else "unavailable",
        content_hash=content_hash, content=content, observed_at=tel.observed_at)
    db.add(snap)
    prev = current_revision(db, incident)
    data = {**prev.data, "evidence_ids": prev.data["evidence_ids"] + [snap.id]}
    _new_revision(db, incident, data, "evidence_added", actor)
    audit.record(db, "evidence_added", actor, incident_id=incident.id, correlation_id=incident.correlation_id,
                 payload={"evidence_id": snap.id, "kind": "telemetry", "available": tel.available})
    if incident.status == "received":
        set_incident_status(db, incident, "enriching", actor, "enrichment_started")
    elif incident.status in ("approved", "dispatching", "blocked"):
        invalidate_approval(db, incident, actor, f"new evidence {snap.id}")
        if incident.status == "needs_review":
            _run_rules(db, incident, actor)
    elif incident.status == "needs_review":
        _run_rules(db, incident, actor)
    db.commit()
    return incident


# --- triage -------------------------------------------------------------------------------------

def evidence_for(db: Session, rev: IncidentRevision) -> list[EvidenceSnapshot]:
    ids = rev.data["evidence_ids"]
    rows = db.execute(select(EvidenceSnapshot).where(EvidenceSnapshot.id.in_(ids))).scalars().all()
    order = {i: n for n, i in enumerate(ids)}
    return sorted(rows, key=lambda e: order[e.id])


def evidence_versions(evidence: list[EvidenceSnapshot]) -> list[dict]:
    return [{"id": e.id, "source_id": e.source_id, "source_version": e.source_version, "content_hash": e.content_hash}
            for e in evidence]


def manifest_id(evidence: list[EvidenceSnapshot]) -> str:
    return "mf-" + sha256_hex(evidence_versions(evidence))[:16]


def validate_refs(proposal: TriageProposal, allowed: set[str], manifest: str, revision: int) -> list[str]:
    """Rejects invented evidence ids, a foreign manifest or a proposal for another revision."""
    errors = []
    if proposal.evidence_manifest_id != manifest:
        errors.append(f"evidence_manifest_id {proposal.evidence_manifest_id!r} != {manifest!r}")
    if proposal.incident_revision != revision:
        errors.append(f"incident_revision {proposal.incident_revision} != {revision}")
    refs = [r for f in proposal.extracted_facts for r in f.evidence_refs]
    refs += [r for c in proposal.contradictions for r in c.evidence_refs]
    errors += [f"unknown evidence ref {r!r}" for r in refs if r not in allowed]
    return errors


def _run_rules(db: Session, incident: Incident, actor: str) -> TriageRun:
    rev = current_revision(db, incident)
    evidence = evidence_for(db, rev)
    message = next(e for e in evidence if e.kind == "message")
    telemetry = next((e for e in reversed(evidence) if e.kind == "telemetry"), None)
    mf = manifest_id(evidence)
    started = time.monotonic()
    input_hash = sha256_hex({"revision": rev.data, "evidence": evidence_versions(evidence)})
    run = TriageRun(incident_id=incident.id, incident_revision=rev.revision, engine=rules.ENGINE,
                    prompt_version=rules.RULES_VERSION, input_hash=input_hash, valid=False, errors=[], duration_ms=0)
    try:
        proposal = rules.run(
            incident_id=str(incident.id), equipment_id=incident.equipment_id, revision=rev.revision, data={
                **rev.data, "category_override": rev.data["overrides"].get("category")},
            message_ref=message.id, telemetry=telemetry.content if telemetry else None,
            telemetry_ref=telemetry.id if telemetry else None, manifest_id=mf,
            reported_at=datetime.fromisoformat(rev.data["reported_at"]), max_age_s=get_settings().telemetry_max_age_s)
        errors = validate_refs(proposal, {e.id for e in evidence}, mf, rev.revision)
        run.proposal = proposal.model_dump(mode="json")
        run.valid, run.errors = not errors, errors
    except Exception as exc:  # the incident must survive any triage failure
        run.errors = [f"{type(exc).__name__}: {exc}"]
    run.duration_ms = int((time.monotonic() - started) * 1000)
    db.add(run)
    db.flush()
    audit.record(db, "triage_completed" if run.valid else "triage_failed", actor, incident_id=incident.id,
                 correlation_id=incident.correlation_id,
                 payload={"engine": run.engine, "revision": rev.revision, "triage_run_id": str(run.id), "errors": run.errors})
    return run


def triage(db: Session, incident_id: uuid.UUID, actor: str) -> TriageRun:
    incident = lock_incident(db, incident_id)
    if incident.status not in ("received", "enriching", "needs_review"):
        raise Conflict("invalid_state", f"cannot triage an incident in state {incident.status}")
    run = _run_rules(db, incident, actor)
    if incident.status != "needs_review":
        set_incident_status(db, incident, "needs_review", actor, "ready_for_review", triage_valid=run.valid)
    db.commit()
    return run


# --- review -------------------------------------------------------------------------------------

def review_state(db: Session, incident: Incident) -> dict:
    """The exact plan a reviewer approves, with its digest. Recomputed server-side at approval."""
    rev = current_revision(db, incident)
    evidence = evidence_for(db, rev)
    run = db.execute(select(TriageRun).where(
        TriageRun.incident_id == incident.id, TriageRun.incident_revision == rev.revision, TriageRun.valid.is_(True)
    ).order_by(TriageRun.created_at.desc()).limit(1)).scalar_one_or_none()
    state = {"revision": rev.revision, "proposal": None, "provenance": None, "plan": None, "plan_digest": None,
             "evidence_versions": evidence_versions(evidence), "policy_version": POLICY_VERSION}
    if run is None:
        return state
    equipment = db.get(Equipment, incident.equipment_id)
    team = db.get(Team, equipment.team_id)
    plan = build_plan(incident_id=str(incident.id), revision=rev.revision, proposal=run.proposal,
                      overrides=rev.data["overrides"], equipment=equipment, team=team)
    state.update(proposal=run.proposal, provenance=run.engine, triage_run_id=str(run.id), plan=plan,
                 plan_digest=plan_digest(incident_id=str(incident.id), revision=rev.revision, plan=plan,
                                         evidence_versions=state["evidence_versions"]))
    return state


def correct(db: Session, incident_id: uuid.UUID, corr: Correction, actor: str) -> Incident:
    incident = lock_incident(db, incident_id)
    if incident.status not in ("needs_review", "approved"):
        raise Conflict("invalid_state", f"cannot correct an incident in state {incident.status}")
    if corr.expected_revision != incident.current_revision:
        raise Conflict("stale_revision", "the incident changed since this page was loaded",
                       current_revision=incident.current_revision)
    prev = current_revision(db, incident)
    overrides = dict(prev.data["overrides"])
    for field in ("category", "ticket_title", "ticket_description"):
        if getattr(corr, field) is not None:
            overrides[field] = getattr(corr, field)
    data = {**prev.data, "overrides": overrides,
            "notes": prev.data["notes"] + [{"by": actor, "at": now(db).isoformat(), "note": corr.note}]}
    _new_revision(db, incident, data, "reviewer_correction", actor)
    if incident.status == "approved":
        invalidate_approval(db, incident, actor, "reviewer correction")
    else:
        set_incident_status(db, incident, "needs_review", actor, "correction_applied")
    _run_rules(db, incident, actor)
    db.commit()
    return incident


def approve(db: Session, incident_id: uuid.UUID, req: ApproveRequest, reviewer: str) -> Approval:
    """Approval, actions and outbox rows are written in one transaction, bound to (revision, digest)."""
    incident = lock_incident(db, incident_id)
    if incident.status != "needs_review":
        raise Conflict("invalid_state", f"cannot approve an incident in state {incident.status}")
    if req.expected_revision != incident.current_revision:
        raise Conflict("stale_revision", "the incident changed since this page was loaded",
                       current_revision=incident.current_revision)
    state = review_state(db, incident)
    if state["plan"] is None:
        raise Conflict("no_valid_proposal", "no valid proposal for the current revision")
    if state["plan_digest"] != req.plan_digest:
        raise Conflict("plan_changed", "the approved plan differs from the current plan", plan_digest=state["plan_digest"])

    approval = Approval(id=uuid.uuid4(), incident_id=incident.id, incident_revision=incident.current_revision,
                        reviewer=reviewer, plan={"actions": state["plan"]}, plan_digest=state["plan_digest"],
                        policy_version=POLICY_VERSION, evidence_versions=state["evidence_versions"])
    db.add(approval)
    db.flush()  # no ORM relationships: flush explicitly so FK targets exist before dependents
    by_type: dict[str, Action] = {}
    for step in state["plan"]:
        action = Action(id=uuid.uuid4(), incident_id=incident.id, approval_id=approval.id, type=step["type"],
                        args=step["args"], status="pending",
                        action_key=f"rf:{incident.id}:r{incident.current_revision}:{step['type']}",
                        depends_on=by_type[step["depends_on"]].id if step.get("depends_on") else None)
        by_type[step["type"]] = action
        db.add(action)
        db.flush()
        # Dependent actions are not eligible until their dependency succeeds.
        db.add(Outbox(action_id=action.id, available_at=None if action.depends_on else now(db)))
        audit.record(db, "action_planned", reviewer, incident_id=incident.id, action_id=action.id,
                     correlation_id=incident.correlation_id, to_state="pending",
                     payload={"type": action.type, "action_key": action.action_key})
    set_incident_status(db, incident, "approved", reviewer, "incident_approved", revision=incident.current_revision,
                        plan_digest=state["plan_digest"], approval_id=str(approval.id))
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise Conflict("duplicate_approval", "this revision is already approved") from exc
    return approval


def reject(db: Session, incident_id: uuid.UUID, req: RejectRequest, reviewer: str) -> Incident:
    incident = lock_incident(db, incident_id)
    if incident.status != "needs_review":
        raise Conflict("invalid_state", f"cannot reject an incident in state {incident.status}")
    if req.expected_revision != incident.current_revision:
        raise Conflict("stale_revision", "the incident changed since this page was loaded",
                       current_revision=incident.current_revision)
    set_incident_status(db, incident, "rejected", reviewer, "incident_rejected", reason=req.reason)
    db.commit()
    return incident
