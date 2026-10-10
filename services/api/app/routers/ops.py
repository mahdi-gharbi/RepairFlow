"""Operations endpoints: recovery sweep (n8n W5), daily digest with a period key (W6), n8n error
reports (error workflow), metrics, and a server-sent event stream of audit events for the dashboard."""
import asyncio
import json
import os

from fastapi import APIRouter, Depends, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import Field
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app import audit, recovery
from app.auth import Principal, principal, require
from app.config import get_settings
from app.contracts import Strict
from app.db import get_db, session
from app.errors import Conflict, NotFound
from app.models import Action, AuditEvent, Digest, Incident
from app.policy import POLICY_VERSION
from app.triage import llm, rules

router = APIRouter(prefix="/v1")
OPS_EMAIL = os.environ.get("RF_OPS_EMAIL", "ops@repairflow.local")  # config, never from content


# --- metrics ------------------------------------------------------------------------------------

def metrics(db: Session) -> dict:
    by_status = dict(db.execute(select(Incident.status, func.count()).group_by(Incident.status)).all())
    actions = dict(db.execute(select(Action.status, func.count()).group_by(Action.status)).all())
    backlog = db.execute(text("""
        SELECT count(*), extract(epoch FROM now() - min(o.available_at))
        FROM outbox o JOIN actions a ON a.id = o.action_id
        WHERE a.status IN ('pending', 'retry_wait') AND o.available_at IS NOT NULL AND o.available_at <= now()""")).one()
    triage = db.execute(text("""
        SELECT engine, valid, count(*) FROM triage_runs WHERE created_at > now() - interval '24 hours'
        GROUP BY engine, valid ORDER BY engine, valid""")).all()
    # Human review time (ready_for_review -> approved) is reported separately from machine dispatch time.
    durations = db.execute(text("""
        WITH r AS (SELECT incident_id, max(created_at) FILTER (WHERE event = 'ready_for_review') AS ready,
                          max(created_at) FILTER (WHERE event = 'incident_approved') AS approved,
                          max(created_at) FILTER (WHERE event = 'incident_completed') AS completed
                   FROM audit_events WHERE incident_id IS NOT NULL GROUP BY incident_id)
        SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM approved - ready))
                 FILTER (WHERE approved > ready),
               percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM completed - approved))
                 FILTER (WHERE completed > approved),
               percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM completed - approved))
                 FILTER (WHERE completed > approved)
        FROM r""")).one()
    return {
        "incidents_by_status": by_status,
        "actions_by_status": actions,
        "eligible_backlog": backlog[0],
        "oldest_eligible_age_s": round(float(backlog[1]), 1) if backlog[1] is not None else None,
        "uncertain_actions": actions.get("uncertain", 0),
        "dead_actions": actions.get("dead", 0),
        "triage_runs_24h": [{"engine": e, "valid": v, "count": c} for e, v, c in triage],
        "review_time_median_s": round(float(durations[0]), 1) if durations[0] is not None else None,
        "dispatch_time_median_s": round(float(durations[1]), 2) if durations[1] is not None else None,
        "dispatch_time_p95_s": round(float(durations[2]), 2) if durations[2] is not None else None,
    }


@router.get("/metrics")
def get_metrics(p: Principal = Depends(principal), db: Session = Depends(get_db)):
    require(p, "operator", "reviewer", "internal")
    return metrics(db)


# --- recovery (W5) ------------------------------------------------------------------------------

@router.post("/ops/recovery")
def run_recovery(p: Principal = Depends(principal)):
    require(p, "internal")
    result = recovery.sweep(f"{p.name}:recovery")
    with session() as db:
        audit.record(db, "recovery_sweep", f"{p.name}:recovery", payload={
            "expired_leases": result["expired_leases"], "reconciled": len(result["reconciled"]),
            "stalled_intake": len(result["stalled_intake"])})
        db.commit()
    return result


# --- digest (W6) --------------------------------------------------------------------------------

class DigestRequest(Strict):
    period_key: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


class DigestSent(Strict):
    message_id: str = Field(max_length=300)


def digest_content(db: Session) -> dict:
    def rows(sql):
        return [dict(r._mapping) for r in db.execute(text(sql))]
    return {
        "pending_review": rows("""SELECT id::text, equipment_id, current_revision AS revision,
                                  round(extract(epoch FROM now() - updated_at) / 60)::int AS age_min
                                  FROM incidents WHERE status = 'needs_review' ORDER BY updated_at LIMIT 50"""),
        "blocked": rows("""SELECT id::text, equipment_id, round(extract(epoch FROM now() - updated_at) / 60)::int AS age_min
                           FROM incidents WHERE status = 'blocked' ORDER BY updated_at LIMIT 50"""),
        "uncertain_or_dead_actions": rows("""SELECT a.id::text, a.incident_id::text, a.type, a.status, o.attempts
                                             FROM actions a JOIN outbox o ON o.action_id = a.id
                                             WHERE a.status IN ('uncertain', 'dead') ORDER BY a.updated_at LIMIT 50"""),
        "metrics": metrics(db),
    }


def digest_text(period: str, c: dict) -> str:
    lines = [f"RepairFlow daily digest {period}", ""]
    lines.append(f"Pending review: {len(c['pending_review'])}")
    lines += [f"  - {r['equipment_id']} incident {r['id'][:8]} rev {r['revision']} waiting {r['age_min']} min" for r in c["pending_review"]]
    lines.append(f"Blocked incidents: {len(c['blocked'])}")
    lines += [f"  - {r['equipment_id']} incident {r['id'][:8]} blocked for {r['age_min']} min" for r in c["blocked"]]
    lines.append(f"Uncertain or dead actions: {len(c['uncertain_or_dead_actions'])}")
    lines += [f"  - {r['type']} {r['status']} (incident {r['incident_id'][:8]}, attempts {r['attempts']})" for r in c["uncertain_or_dead_actions"]]
    m = c["metrics"]
    lines += ["", f"Eligible backlog: {m['eligible_backlog']} (oldest {m['oldest_eligible_age_s']} s)",
              f"Median review time: {m['review_time_median_s']} s; median dispatch time: {m['dispatch_time_median_s']} s"]
    return "\n".join(lines)


@router.post("/ops/digests")
def reserve_digest(req: DigestRequest, p: Principal = Depends(principal), db: Session = Depends(get_db)):
    """Reserve the period key. Already sent -> send=false. Reserved but not sent -> may be retried."""
    require(p, "internal")
    content = digest_content(db)
    inserted = db.execute(insert(Digest).values(period_key=req.period_key, status="reserved", content=content)
                          .on_conflict_do_nothing().returning(Digest.period_key)).scalar_one_or_none()
    d = db.execute(select(Digest).where(Digest.period_key == req.period_key).with_for_update()).scalar_one()
    if d.status == "sent":
        db.rollback()
        return {"period_key": d.period_key, "send": False, "reason": "already_sent", "message_id": d.message_id}
    if inserted is None:
        d.attempts += 1
        d.content = content
    audit.record(db, "digest_reserved", p.name, payload={"period_key": req.period_key, "attempt": d.attempts})
    db.commit()
    return {"period_key": d.period_key, "send": True, "attempt": d.attempts, "to": OPS_EMAIL,
            "subject": f"[RepairFlow] Daily digest {d.period_key} [RF-DIGEST-{d.period_key}]",
            "text": digest_text(d.period_key, content)}


@router.post("/ops/digests/{period_key}/sent")
def digest_sent(period_key: str, req: DigestSent, p: Principal = Depends(principal), db: Session = Depends(get_db)):
    require(p, "internal")
    d = db.execute(select(Digest).where(Digest.period_key == period_key).with_for_update()).scalar_one_or_none()
    if d is None:
        raise NotFound("digest_not_found", period_key)
    if d.status == "sent":
        if d.message_id == req.message_id:
            return {"status": "sent", "replayed": True}
        raise Conflict("digest_already_sent", "a different message was already recorded for this period")
    d.status, d.message_id, d.sent_at = "sent", req.message_id, func.now()
    audit.record(db, "digest_sent", p.name, payload={"period_key": period_key})
    db.commit()
    return {"status": "sent", "replayed": False}


# --- n8n error workflow -------------------------------------------------------------------------

class N8nError(Strict):
    workflow_id: str | None = Field(default=None, max_length=64)
    workflow_name: str | None = Field(default=None, max_length=200)
    execution_id: str | None = Field(default=None, max_length=64)
    mode: str | None = Field(default=None, max_length=32)
    last_node: str | None = Field(default=None, max_length=200)
    message: str | None = Field(default=None, max_length=1000)


@router.post("/ops/n8n-errors")
def n8n_error(err: N8nError, p: Principal = Depends(principal), db: Session = Depends(get_db)):
    require(p, "internal")
    audit.record(db, "n8n_execution_failed", p.name, payload=err.model_dump())
    db.commit()
    return {"recorded": True}


# --- events (SSE) -------------------------------------------------------------------------------

def _events_after(after: int, limit: int = 200) -> list[dict]:
    with session() as db:
        rows = db.execute(select(AuditEvent).where(AuditEvent.id > after).order_by(AuditEvent.id).limit(limit)).scalars().all()
    return [{"id": r.id, "at": r.created_at.isoformat(), "incident_id": str(r.incident_id) if r.incident_id else None,
             "action_id": str(r.action_id) if r.action_id else None, "event": r.event, "from": r.from_state,
             "to": r.to_state} for r in rows]


def _last_event_id() -> int:
    with session() as db:
        return db.execute(select(func.coalesce(func.max(AuditEvent.id), 0))).scalar_one()


@router.get("/events")
async def events(request: Request, after: int | None = Query(default=None, ge=0), p: Principal = Depends(principal)):
    """Server-sent events of audit rows (ids are monotonic). The dashboard reads it with fetch() so the
    bearer token travels in a header; `after` (or Last-Event-ID) resumes without gaps."""
    require(p, "operator", "reviewer")
    header = request.headers.get("last-event-id")
    cursor = after if after is not None else (int(header) if header and header.isdigit() else await run_in_threadpool(_last_event_id))

    async def stream():
        nonlocal cursor
        idle = 0.0
        yield "retry: 2000\n\n"
        while not await request.is_disconnected():
            batch = await run_in_threadpool(_events_after, cursor)
            for e in batch:
                cursor = e["id"]
                yield f"id: {e['id']}\nevent: audit\ndata: {json.dumps(e)}\n\n"
            if batch:
                idle = 0.0
            else:
                idle += 1.0
                if idle >= 15:
                    idle = 0.0
                    yield ": keep-alive\n\n"
            await asyncio.sleep(1.0)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# --- diagnostics export -------------------------------------------------------------------------

SAFE_PAYLOAD_KEYS = ("outcome", "http_status", "attempt", "fencing_token", "engine", "model", "reason", "finding",
                     "retry_in_s", "revision", "provenance", "type", "decision", "scenario", "mode", "workflow_name",
                     "last_node", "period_key")
FAILURE_EVENTS = ("triage_failed", "llm_fallback_to_rules", "action_outcome_unknown", "action_dead_permanent",
                  "action_dead_max_attempts", "execution_lease_expired", "dispatch_not_received", "n8n_execution_failed",
                  "begin_rejected_stale_token", "ack_rejected_stale_token", "approval_superseded_after_start",
                  "reconciliation_inconclusive", "intake_conflict")


@router.get("/ops/diagnostics")
def diagnostics(p: Principal = Depends(principal), db: Session = Depends(get_db)):
    """Shareable diagnostic bundle. Allow-list only: versions, counts, metrics, recent event ids and
    transitions. Never report text, evidence content, prompts, model output or secrets."""
    require(p, "reviewer")
    s = get_settings()
    recent = db.execute(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(200)).scalars().all()
    failures = db.execute(select(AuditEvent).where(AuditEvent.event.in_(FAILURE_EVENTS))
                          .order_by(AuditEvent.id.desc()).limit(50)).scalars().all()

    def event(e: AuditEvent, with_payload: bool) -> dict:
        out = {"id": e.id, "at": e.created_at.isoformat(), "event": e.event, "actor": e.actor.split(":")[0],
               "incident_id": str(e.incident_id) if e.incident_id else None,
               "action_id": str(e.action_id) if e.action_id else None, "from": e.from_state, "to": e.to_state}
        if with_payload:
            out["payload"] = {k: (str(v)[:200] if isinstance(v, str) else v) for k, v in (e.payload or {}).items()
                              if k in SAFE_PAYLOAD_KEYS}
            if e.payload and e.payload.get("errors"):
                out["payload"]["error_kinds"] = sorted({str(x).split(":")[0][:60] for x in e.payload["errors"]})
        return out

    return {
        "generated_at": db.execute(text("SELECT now()")).scalar_one().isoformat(),
        "versions": {
            "schema": db.execute(text("SELECT version_num FROM alembic_version")).scalar_one(),
            "policy": POLICY_VERSION, "rules": rules.RULES_VERSION, "prompt": llm.prompt_version(),
            "llm": {"configured": llm.enabled(), "model": s.llm_model or None,
                    "provider_host": s.llm_base_url.split("/")[2] if "://" in s.llm_base_url else None,
                    "provider_order": s.llm_provider_order or None},
        },
        "metrics": metrics(db),
        "recent_events": [event(e, False) for e in recent],
        "recent_failures": [event(e, True) for e in failures],
    }
