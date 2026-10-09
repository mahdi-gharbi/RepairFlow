"""Recovery sweep shared by the worker loop and the n8n W5 workflow (POST /v1/ops/recovery).

Running both is safe: every step locks rows (SKIP LOCKED / FOR UPDATE) and re-checks state, so the
durable worker keeps recovery working even when the n8n schedule is not running.
"""
from sqlalchemy import select

from app import dispatch, incidents
from app.db import session
from app.errors import DomainError
from app.models import Action

STALLED_INTAKE_S = 120


def reconcile_uncertain(actor: str, ids=None) -> list[dict]:
    if ids is None:
        with session() as db:
            ids = db.execute(select(Action.id).where(Action.status == "uncertain",
                                                     Action.type.in_(dispatch.RECONCILABLE))).scalars().all()
    out = []
    for action_id in ids:
        result = dispatch.reconcile(session, action_id, actor)
        out.append({"action_id": str(action_id), "status": result["status"], "finding": result["finding"]})
    return out


def recover_stalled_intake(actor: str, older_than_s: int = STALLED_INTAKE_S) -> list[dict]:
    """An intake workflow that died mid-way leaves the incident in received/enriching. The report is
    kept; rules triage runs on what exists and flags the missing evidence for the reviewer."""
    with session() as db:
        ids = incidents.stalled_enrichment(db, older_than_s)
    out = []
    for incident_id in ids:
        try:
            run = incidents.triage(session, incident_id, actor, engine="rules")
            out.append({"incident_id": str(incident_id), "recovered": True, "triage_valid": run.valid})
        except DomainError as exc:  # state changed concurrently
            out.append({"incident_id": str(incident_id), "recovered": False, "reason": exc.code})
    return out


def sweep(actor: str, stalled_after_s: int = STALLED_INTAKE_S) -> dict:
    with session() as db:
        expired = dispatch.sweep_expired(db, actor)
    return {
        "expired_leases": len(expired),
        "reconciled": reconcile_uncertain(actor),
        "stalled_intake": recover_stalled_intake(actor, stalled_after_s),
    }
