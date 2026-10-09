import uuid

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import dispatch, incidents
from app.auth import Principal, principal, require
from app.contracts import (
    AckRequest, ApproveRequest, BeginRequest, Correction, IntakeRequest, RejectRequest, ResolveRequest, TelemetryEvidence,
)
from app.db import get_db, session
from app.errors import NotFound
from app.models import Action, AuditEvent, Equipment, EvidenceSnapshot, Incident, Outbox, TriageRun

router = APIRouter(prefix="/v1")


def _incident_summary(i: Incident) -> dict:
    return {"id": str(i.id), "equipment_id": i.equipment_id, "status": i.status, "revision": i.current_revision,
            "correlation_id": str(i.correlation_id), "created_at": i.created_at, "updated_at": i.updated_at}


def _action_view(a: Action, ob: Outbox | None) -> dict:
    return {"id": str(a.id), "type": a.type, "status": a.status, "action_key": a.action_key, "args": a.args,
            "provider_reference": a.provider_reference, "last_error": a.last_error,
            "attempts": ob.attempts if ob else None, "offers": ob.offers if ob else None,
            "fencing_token": ob.fencing_token if ob else None,
            "available_at": ob.available_at if ob else None, "approval_id": str(a.approval_id)}


@router.post("/incidents")
def create_incident(req: IntakeRequest, response: Response, p: Principal = Depends(principal), db: Session = Depends(get_db)):
    require(p, "internal")
    status, incident = incidents.intake(db, req, p.name)
    response.status_code = status
    return {"incident_id": str(incident.id), "status": incident.status, "replayed": status == 200,
            "correlation_id": str(incident.correlation_id), "equipment_id": incident.equipment_id}


@router.post("/incidents/{incident_id}/evidence/telemetry")
def add_telemetry(incident_id: uuid.UUID, tel: TelemetryEvidence, p: Principal = Depends(principal), db: Session = Depends(get_db)):
    require(p, "internal")
    incident = incidents.add_telemetry(db, incident_id, tel, p.name)
    return _incident_summary(incident)


@router.post("/incidents/{incident_id}/triage")
def run_triage(incident_id: uuid.UUID, p: Principal = Depends(principal), db: Session = Depends(get_db)):
    require(p, "internal", "reviewer")
    run = incidents.triage(db, incident_id, p.name)
    return {"triage_run_id": str(run.id), "engine": run.engine, "valid": run.valid, "errors": run.errors,
            "revision": run.incident_revision, "suggested_next_step": (run.proposal or {}).get("suggested_next_step")}


@router.get("/incidents")
def list_incidents(status: str | None = None, limit: int = Query(50, le=200), p: Principal = Depends(principal),
                   db: Session = Depends(get_db)):
    require(p, "operator", "reviewer")
    q = select(Incident).order_by(Incident.created_at.desc()).limit(limit)
    if status:
        q = q.where(Incident.status == status)
    return [_incident_summary(i) for i in db.execute(q).scalars()]


@router.get("/incidents/{incident_id}")
def get_incident(incident_id: uuid.UUID, p: Principal = Depends(principal), db: Session = Depends(get_db)):
    require(p, "operator", "reviewer", "internal")
    incident = db.get(Incident, incident_id)
    if incident is None:
        raise NotFound("incident_not_found", str(incident_id))
    rev = incidents.current_revision(db, incident)
    evidence = incidents.evidence_for(db, rev)
    actions = db.execute(select(Action, Outbox).join(Outbox, Outbox.action_id == Action.id)
                         .where(Action.incident_id == incident.id).order_by(Action.created_at)).all()
    equipment = db.get(Equipment, incident.equipment_id)
    return {
        **_incident_summary(incident),
        "equipment": {"id": equipment.id, "model": equipment.model, "display_name": equipment.display_name, "site": equipment.site},
        "revision_data": rev.data,
        "evidence": [{"id": e.id, "kind": e.kind, "source_id": e.source_id, "source_version": e.source_version,
                      "observed_at": e.observed_at, "retrieved_at": e.retrieved_at, "content": e.content} for e in evidence],
        "review": incidents.review_state(db, incident),
        "actions": [_action_view(a, ob) for a, ob in actions],
    }


@router.get("/incidents/{incident_id}/timeline")
def timeline(incident_id: uuid.UUID, p: Principal = Depends(principal), db: Session = Depends(get_db)):
    require(p, "operator", "reviewer", "internal")
    rows = db.execute(select(AuditEvent).where(AuditEvent.incident_id == incident_id).order_by(AuditEvent.id)).scalars()
    return [{"id": r.id, "at": r.created_at, "event": r.event, "actor": r.actor, "action_id": str(r.action_id) if r.action_id else None,
             "from": r.from_state, "to": r.to_state, "payload": r.payload} for r in rows]


@router.get("/incidents/{incident_id}/triage-runs")
def triage_runs(incident_id: uuid.UUID, p: Principal = Depends(principal), db: Session = Depends(get_db)):
    require(p, "reviewer", "internal")
    rows = db.execute(select(TriageRun).where(TriageRun.incident_id == incident_id).order_by(TriageRun.created_at)).scalars()
    return [{"id": str(r.id), "revision": r.incident_revision, "engine": r.engine, "model": r.model, "prompt_version": r.prompt_version,
             "valid": r.valid, "errors": r.errors, "duration_ms": r.duration_ms, "input_hash": r.input_hash} for r in rows]


@router.post("/incidents/{incident_id}/corrections")
def correct(incident_id: uuid.UUID, corr: Correction, p: Principal = Depends(principal), db: Session = Depends(get_db)):
    require(p, "reviewer")
    return _incident_summary(incidents.correct(db, incident_id, corr, p.name))


@router.post("/incidents/{incident_id}/approve")
def approve(incident_id: uuid.UUID, req: ApproveRequest, p: Principal = Depends(principal), db: Session = Depends(get_db)):
    require(p, "reviewer")
    approval = incidents.approve(db, incident_id, req, p.name)
    return {"approval_id": str(approval.id), "revision": approval.incident_revision, "plan_digest": approval.plan_digest}


@router.post("/incidents/{incident_id}/reject")
def reject(incident_id: uuid.UUID, req: RejectRequest, p: Principal = Depends(principal), db: Session = Depends(get_db)):
    require(p, "reviewer")
    return _incident_summary(incidents.reject(db, incident_id, req, p.name))


@router.post("/actions/{action_id}/begin")
def begin(action_id: uuid.UUID, req: BeginRequest, p: Principal = Depends(principal), db: Session = Depends(get_db)):
    require(p, "internal")
    return dispatch.begin(db, action_id, req, p.name)


@router.post("/actions/{action_id}/ack")
def ack(action_id: uuid.UUID, req: AckRequest, p: Principal = Depends(principal), db: Session = Depends(get_db)):
    require(p, "internal")
    return dispatch.ack(db, action_id, req, p.name)


@router.post("/actions/{action_id}/reconcile")
def reconcile(action_id: uuid.UUID, p: Principal = Depends(principal)):
    require(p, "internal", "reviewer")
    return dispatch.reconcile(session, action_id, p.name)


@router.post("/actions/{action_id}/resolve")
def resolve(action_id: uuid.UUID, req: ResolveRequest, p: Principal = Depends(principal), db: Session = Depends(get_db)):
    require(p, "reviewer")
    return dispatch.resolve(db, action_id, req, p.name)


@router.get("/evidence/{evidence_id}")
def get_evidence(evidence_id: str, p: Principal = Depends(principal), db: Session = Depends(get_db)):
    require(p, "operator", "reviewer")
    e = db.get(EvidenceSnapshot, evidence_id)
    if e is None:
        raise NotFound("evidence_not_found", evidence_id)
    return {"id": e.id, "kind": e.kind, "source_id": e.source_id, "source_version": e.source_version,
            "content_hash": e.content_hash, "observed_at": e.observed_at, "retrieved_at": e.retrieved_at, "content": e.content}
