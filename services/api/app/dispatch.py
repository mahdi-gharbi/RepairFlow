"""Outbox claiming, begin/ack protocol, lease expiry and reconciliation.

Guarantees (see docs/reliability.md): no transaction is held open across a network call; a stale
worker or callback cannot overwrite newer state (fencing token); an effect whose outcome is unknown
becomes `uncertain` and is only retried when the provider offers a by-key lookup (tickets), never
blindly (SMTP). There is no global exactly-once guarantee.
"""
import random
import secrets
import uuid
from datetime import timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit
from app.canonical import sha256_hex
from app.config import get_settings
from app.contracts import AckRequest, BeginRequest, ResolveRequest
from app.errors import Conflict, Invalid, NotFound
from app.incidents import lock_incident, now, set_action_status, set_incident_status
from app.models import Action, Approval, Incident, Outbox

OFFER_LEASE = timedelta(seconds=30)  # time for n8n to call /begin after the webhook
# Time for the connector call + /ack, derived from each connector's real timeout: the ticket HTTP
# node times out after 10 s; n8n's SMTP node exposes no timeout and nodemailer waits up to 120 s
# to connect. An attempt outliving its lease becomes `uncertain`; a late ack of it still resolves it.
EXECUTING_LEASE = {"create_ticket": timedelta(seconds=60), "notify_team": timedelta(seconds=180)}
MAX_ATTEMPTS = 5
BACKOFF_BASE_S, BACKOFF_CAP_S = 2.0, 300.0
# Connectors with an idempotent by-key lookup can be reconciled and retried automatically.
RECONCILABLE = {"create_ticket"}


def backoff(attempts: int) -> float:
    """Exponential backoff with full jitter in [0.5, 1.0] of the step."""
    return min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2 ** max(attempts - 1, 0)) * random.uniform(0.5, 1.0)


def email_marker(action: Action) -> str:
    return f"RF-{action.id.hex[:12]}"


def lock_action(db: Session, action_id: uuid.UUID) -> tuple[Outbox, Action]:
    ob = db.execute(select(Outbox).where(Outbox.action_id == action_id).with_for_update()).scalar_one_or_none()
    if ob is None:
        raise NotFound("action_not_found", str(action_id))
    action = db.execute(select(Action).where(Action.id == action_id).with_for_update()).scalar_one()
    return ob, action


def _lock_for_callback(db: Session, action_id: uuid.UUID) -> tuple[Incident, Outbox, Action]:
    action = db.get(Action, action_id)
    if action is None:
        raise NotFound("action_not_found", str(action_id))
    incident = lock_incident(db, action.incident_id)
    db.refresh(action)
    ob, action = lock_action(db, action_id)
    return incident, ob, action


def _token_ok(ob: Outbox, fencing: int, token: str) -> bool:
    return (fencing == ob.fencing_token and ob.dispatch_token_hash is not None
            and secrets.compare_digest(ob.dispatch_token_hash, sha256_hex(token)))


# --- worker side --------------------------------------------------------------------------------

def claim(db: Session, owner: str, limit: int = 10) -> list[dict]:
    """Short transaction: lock eligible rows (SKIP LOCKED), lease them, bump the fencing token, commit.
    The caller performs the network call only after this returns."""
    t = now(db)
    rows = db.execute(
        select(Outbox).join(Action, Action.id == Outbox.action_id)
        .where(Action.status.in_(("pending", "retry_wait")), Outbox.available_at.is_not(None), Outbox.available_at <= t)
        .order_by(Outbox.available_at).limit(limit)
        .with_for_update(of=Outbox, skip_locked=True)
    ).scalars().all()
    offers = []
    for ob in rows:
        action = db.execute(select(Action).where(Action.id == ob.action_id).with_for_update()).scalar_one()
        if action.status not in ("pending", "retry_wait"):
            continue
        token = secrets.token_urlsafe(24)
        ob.fencing_token += 1
        ob.offers += 1
        ob.lease_owner, ob.lease_until, ob.dispatch_token_hash = owner, t + OFFER_LEASE, sha256_hex(token)
        incident = db.get(Incident, action.incident_id)
        set_action_status(db, action, "offered", owner, "action_offered", incident.correlation_id,
                          fencing_token=ob.fencing_token, offer=ob.offers)
        offers.append({"action_id": str(action.id), "type": action.type, "fencing_token": ob.fencing_token,
                       "dispatch_token": token, "incident_id": str(incident.id),
                       "correlation_id": str(incident.correlation_id)})
    db.commit()
    return offers


def release_offer(db: Session, action_id: uuid.UUID, fencing: int, owner: str, reason: str) -> None:
    """The dispatch webhook provably did not receive the command (connection refused, 404): requeue."""
    ob, action = lock_action(db, action_id)
    if ob.fencing_token != fencing or action.status != "offered":
        db.rollback()
        return
    delay = backoff(ob.offers)
    ob.available_at, ob.lease_owner, ob.lease_until = now(db) + timedelta(seconds=delay), None, None
    set_action_status(db, action, "retry_wait", owner, "dispatch_not_received", reason=reason, retry_in_s=round(delay, 1))
    db.commit()


def sweep_expired(db: Session, owner: str) -> list[uuid.UUID]:
    """Lease expiry. offered: begin never happened, so no effect started -> back to pending.
    executing: the effect may have happened -> uncertain (reconciled or escalated, never blindly retried)."""
    t = now(db)
    expired = db.execute(
        select(Outbox).join(Action, Action.id == Outbox.action_id)
        .where(Action.status.in_(("offered", "executing")), Outbox.lease_until < t)
        .with_for_update(of=Outbox, skip_locked=True)
    ).scalars().all()
    to_reconcile = []
    for ob in expired:
        action = db.execute(select(Action).where(Action.id == ob.action_id).with_for_update()).scalar_one()
        incident = db.get(Incident, action.incident_id)
        if action.status == "offered":
            ob.available_at, ob.lease_owner, ob.lease_until = t, None, None
            set_action_status(db, action, "pending", owner, "offer_lease_expired", incident.correlation_id,
                              fencing_token=ob.fencing_token)
        elif action.status == "executing":
            ob.lease_owner, ob.lease_until = None, None
            set_action_status(db, action, "uncertain", owner, "execution_lease_expired", incident.correlation_id,
                              fencing_token=ob.fencing_token, attempt=ob.attempts)
            _after_uncertain(db, incident, action, owner)
            if action.type in RECONCILABLE:
                to_reconcile.append(action.id)
    db.commit()
    return to_reconcile


# --- workflow callbacks -------------------------------------------------------------------------

def begin(db: Session, action_id: uuid.UUID, req: BeginRequest, actor: str) -> dict:
    """Atomic compare-and-set offered -> executing. Returns the frozen, approved arguments only."""
    incident, ob, action = _lock_for_callback(db, action_id)
    if not _token_ok(ob, req.fencing_token, req.dispatch_token):
        audit.record(db, "begin_rejected_stale_token", actor, incident_id=incident.id, action_id=action.id,
                     correlation_id=incident.correlation_id,
                     payload={"given_fencing": req.fencing_token, "current_fencing": ob.fencing_token})
        db.commit()
        raise Conflict("stale_fencing", "this dispatch token is no longer valid")
    if action.status != "offered":
        db.rollback()
        raise Conflict("not_offered", f"action is {action.status}")
    if ob.lease_until is None or ob.lease_until < now(db):
        db.rollback()
        raise Conflict("lease_expired", "offer lease expired before begin")

    approval = db.get(Approval, action.approval_id)
    if approval.incident_revision != incident.current_revision or incident.status not in ("approved", "dispatching", "blocked"):
        ob.available_at, ob.lease_owner, ob.lease_until = None, None, None
        set_action_status(db, action, "cancelled", actor, "begin_refused_stale_approval", incident.correlation_id,
                          approved_revision=approval.incident_revision, current_revision=incident.current_revision)
        db.commit()
        raise Conflict("approval_stale", "the approval no longer matches the current incident revision")

    context = {}
    if action.depends_on:
        dep = db.get(Action, action.depends_on)
        if dep.status != "succeeded":
            db.rollback()
            raise Conflict("dependency_not_satisfied", f"dependency is {dep.status}")
        context["ticket_reference"] = dep.provider_reference
    if action.type == "notify_team":
        context["message_marker"] = email_marker(action)

    ob.attempts += 1
    ob.lease_until = now(db) + EXECUTING_LEASE[action.type]
    set_action_status(db, action, "executing", actor, "action_begin", incident.correlation_id,
                      fencing_token=ob.fencing_token, attempt=ob.attempts)
    if incident.status in ("approved", "blocked"):
        set_incident_status(db, incident, "dispatching", actor, "dispatch_started", action_id=str(action.id))
    db.commit()
    return {"action_id": str(action.id), "type": action.type, "action_key": action.action_key, "args": action.args,
            "context": context, "attempt": ob.attempts, "fencing_token": ob.fencing_token,
            "correlation_id": str(incident.correlation_id)}


def ack(db: Session, action_id: uuid.UUID, req: AckRequest, actor: str) -> dict:
    incident, ob, action = _lock_for_callback(db, action_id)
    if not _token_ok(ob, req.fencing_token, req.dispatch_token):
        audit.record(db, "ack_rejected_stale_token", actor, incident_id=incident.id, action_id=action.id,
                     correlation_id=incident.correlation_id,
                     payload={"given_fencing": req.fencing_token, "current_fencing": ob.fencing_token, "outcome": req.outcome})
        db.commit()
        raise Conflict("stale_fencing", "acknowledgement from an outdated attempt")
    if action.status == "succeeded" and req.outcome == "succeeded" and req.provider_reference == action.provider_reference:
        db.rollback()
        return {"status": action.status, "replayed": True}
    # `uncertain` with the same fencing token = a late ack for the very attempt that timed out.
    if action.status not in ("executing", "uncertain"):
        db.rollback()
        raise Conflict("not_executing", f"action is {action.status}")

    evidence = {"outcome": req.outcome, "http_status": req.http_status, "provider_reference": req.provider_reference,
                "detail": req.detail, "attempt": ob.attempts, "fencing_token": ob.fencing_token}
    late = action.status == "uncertain"
    ob.lease_owner, ob.lease_until = None, None

    if req.outcome == "succeeded":
        if not req.provider_reference:
            db.rollback()
            raise Invalid("missing_provider_reference", "a success needs the provider reference")
        _succeed(db, incident, action, req.provider_reference, actor, "action_succeeded_late_ack" if late else "action_succeeded", evidence)
    elif late:
        # The attempt is already escalated; a non-success late ack adds evidence, not a decision.
        audit.record(db, "late_ack_recorded", actor, incident_id=incident.id, action_id=action.id,
                     correlation_id=incident.correlation_id, payload=evidence)
    elif req.outcome in ("retryable", "rate_limited"):
        action.last_error = evidence
        if ob.attempts >= MAX_ATTEMPTS:
            set_action_status(db, action, "dead", actor, "action_dead_max_attempts", incident.correlation_id, **evidence)
            _block(db, incident, actor, action)
        else:
            delay = req.retry_after_s if (req.outcome == "rate_limited" and req.retry_after_s is not None) else backoff(ob.attempts)
            ob.available_at = now(db) + timedelta(seconds=delay)
            set_action_status(db, action, "retry_wait", actor, "action_retry_scheduled", incident.correlation_id,
                              retry_in_s=round(delay, 1), **evidence)
    elif req.outcome == "permanent":
        action.last_error = evidence
        set_action_status(db, action, "dead", actor, "action_dead_permanent", incident.correlation_id, **evidence)
        _block(db, incident, actor, action)
    else:  # ambiguous: the effect may or may not have happened
        action.last_error = evidence
        set_action_status(db, action, "uncertain", actor, "action_outcome_unknown", incident.correlation_id, **evidence)
        _after_uncertain(db, incident, action, actor)
    db.commit()
    return {"status": action.status, "replayed": False}


def _succeed(db: Session, incident: Incident, action: Action, reference: str, actor: str, event: str, evidence: dict) -> None:
    action.provider_reference = reference
    set_action_status(db, action, "succeeded", actor, event, incident.correlation_id, **evidence)
    for dep in db.execute(select(Action).where(Action.depends_on == action.id, Action.status == "pending")).scalars():
        dep_ob = db.execute(select(Outbox).where(Outbox.action_id == dep.id).with_for_update()).scalar_one()
        dep_ob.available_at = now(db)
        audit.record(db, "action_released", actor, incident_id=incident.id, action_id=dep.id,
                     correlation_id=incident.correlation_id, payload={"dependency": str(action.id)})
    remaining = db.execute(select(Action).where(
        Action.approval_id == action.approval_id, Action.status.notin_(("succeeded", "cancelled")))).scalars().all()
    if not remaining and incident.status in ("dispatching", "blocked"):
        set_incident_status(db, incident, "completed", actor, "incident_completed")


def _block(db: Session, incident: Incident, actor: str, action: Action) -> None:
    if incident.status in ("approved", "dispatching"):
        set_incident_status(db, incident, "blocked", actor, "incident_blocked", action_id=str(action.id), action_status=action.status)


def _after_uncertain(db: Session, incident: Incident, action: Action, actor: str) -> None:
    if action.type not in RECONCILABLE:
        _block(db, incident, actor, action)


# --- reconciliation -----------------------------------------------------------------------------

def lookup_ticket(action_key: str) -> tuple[str, str | None]:
    s = get_settings()
    try:
        r = httpx.get(f"{s.ticket_service_url}/tickets/by-key/{action_key}", headers={"X-Api-Key": s.ticket_api_key}, timeout=5)
    except httpx.HTTPError as exc:
        return "error", f"{type(exc).__name__}"
    if r.status_code == 200:
        return "found", r.json()["ticket_id"]
    if r.status_code == 404:
        return "absent", None
    return "error", f"HTTP {r.status_code}"


def lookup_email(marker: str) -> tuple[str, str | None]:
    """Demo-only reconciliation against Mailpit's API. A real SMTP relay offers no such lookup."""
    try:
        r = httpx.get(f"{get_settings().mailpit_url}/api/v1/search", params={"query": f'subject:"{marker}"'}, timeout=5)
    except httpx.HTTPError as exc:
        return "error", f"{type(exc).__name__}"
    if r.status_code != 200:
        return "error", f"HTTP {r.status_code}"
    messages = r.json().get("messages") or []
    return ("found", messages[0].get("MessageID") or messages[0].get("ID")) if messages else ("absent", None)


def reconcile(session_factory, action_id: uuid.UUID, actor: str) -> dict:
    """Lookup happens outside any transaction; the decision re-checks state under lock."""
    with session_factory() as db:
        action = db.get(Action, action_id)
        if action is None:
            raise NotFound("action_not_found", str(action_id))
        if action.status != "uncertain":
            return {"status": action.status, "finding": "not_uncertain"}
        ob = db.execute(select(Outbox).where(Outbox.action_id == action_id)).scalar_one()
        fencing, kind, key, marker = ob.fencing_token, action.type, action.action_key, email_marker(action)

    finding, reference = lookup_ticket(key) if kind == "create_ticket" else lookup_email(marker)

    with session_factory() as db:
        incident, ob, action = _lock_for_callback(db, action_id)
        if action.status != "uncertain" or ob.fencing_token != fencing:
            db.rollback()
            return {"status": action.status, "finding": "changed_concurrently"}
        payload = {"finding": finding, "provider_reference": reference, "lookup": kind}
        if finding == "found":
            _succeed(db, incident, action, reference, actor, "reconciled_found", payload)
        elif finding == "absent" and kind in RECONCILABLE:
            # Safe to re-dispatch: the ticket provider deduplicates on Idempotency-Key.
            ob.available_at = now(db)
            set_action_status(db, action, "retry_wait", actor, "reconciled_absent_retry", incident.correlation_id, **payload)
        else:
            # Email absent from Mailpit or lookup error: evidence for a human, not an automatic retry.
            audit.record(db, "reconciliation_inconclusive", actor, incident_id=incident.id, action_id=action.id,
                         correlation_id=incident.correlation_id, payload=payload)
        db.commit()
        return {"status": action.status, "finding": finding, "provider_reference": reference}


def resolve(db: Session, action_id: uuid.UUID, req: ResolveRequest, reviewer: str) -> dict:
    """Human decision for uncertain/dead actions. 'retry' on an email explicitly accepts duplicate risk."""
    incident, ob, action = _lock_for_callback(db, action_id)
    if action.status not in ("uncertain", "dead"):
        db.rollback()
        raise Conflict("not_resolvable", f"action is {action.status}")
    payload = {"decision": req.decision, "note": req.note}
    if req.decision == "mark_succeeded":
        if not req.provider_reference:
            db.rollback()
            raise Invalid("missing_provider_reference", "give the reference observed at the provider")
        _succeed(db, incident, action, req.provider_reference, reviewer, "resolved_succeeded", payload)
    elif req.decision == "retry":
        ob.available_at = now(db)
        set_action_status(db, action, "retry_wait", reviewer, "resolved_retry", incident.correlation_id, **payload)
    else:
        if action.status == "uncertain":
            set_action_status(db, action, "dead", reviewer, "resolved_give_up", incident.correlation_id, **payload)
        else:
            audit.record(db, "resolved_give_up", reviewer, incident_id=incident.id, action_id=action.id,
                         correlation_id=incident.correlation_id, payload=payload)
        _block(db, incident, reviewer, action)
    db.commit()
    return {"status": action.status}
