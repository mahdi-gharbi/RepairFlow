"""Reliability properties against the real PostgreSQL + API containers.

The worker container is stopped for this module so the test plays the worker's role (claim) and
controls timing. Lease expiry is forced by moving `lease_until` into the past: that simulates the
passage of time, not a different code path.
"""
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from sqlalchemy import func, select, text, update

from app import dispatch
from app.db import session
from app.models import Action, InboxEvent, Outbox
from helpers import (
    API, INTERNAL, OPERATOR, REVIEWER, TICKET_KEY, TICKETS, action, approve, compose, incident, intake_api, ready_incident,
    report, tickets_for_key, timeline,
)

OWNER = "pytest-worker"


@pytest.fixture(scope="module", autouse=True)
def worker_stopped():
    compose("stop", "worker")
    yield
    compose("start", "worker")


def claim_for(iid: str, kind: str) -> dict:
    with session() as db:
        offers = dispatch.claim(db, OWNER, limit=50)
    mine = [o for o in offers if o["incident_id"] == iid and o["type"] == kind]
    assert mine, f"no {kind} offer for {iid}: {offers}"
    # Release offers of other incidents so they do not leak between tests.
    return mine[0]


def begin(offer, fencing=None, token=None) -> httpx.Response:
    return httpx.post(f"{API}/v1/actions/{offer['action_id']}/begin", headers=INTERNAL, timeout=10, json={
        "fencing_token": offer["fencing_token"] if fencing is None else fencing,
        "dispatch_token": token or offer["dispatch_token"]})


def ack(offer, outcome, ref=None, fencing=None, **kw) -> httpx.Response:
    return httpx.post(f"{API}/v1/actions/{offer['action_id']}/ack", headers=INTERNAL, timeout=10, json={
        "fencing_token": offer["fencing_token"] if fencing is None else fencing,
        "dispatch_token": offer["dispatch_token"], "outcome": outcome, "provider_reference": ref, **kw})


def expire_lease(action_id: str) -> None:
    with session() as db:
        db.execute(update(Outbox).where(Outbox.action_id == uuid.UUID(action_id))
                   .values(lease_until=text("now() - interval '1 second'")))
        db.commit()


def create_ticket_like_n8n(begun: dict) -> httpx.Response:
    a = begun["args"]
    return httpx.post(f"{TICKETS}/tickets", headers={**TICKET_KEY, "Idempotency-Key": begun["action_key"]}, timeout=10, json={
        "title": a["title"], "description": a["description"], "priority": a["priority"], "equipment_id": a["equipment_id"],
        "site": a["site"], "category": a["category"], "external_reference": a["incident_id"]})


# --- intake idempotency -------------------------------------------------------------------------

def test_20_concurrent_identical_intakes_create_one_incident():
    rep = report()
    with ThreadPoolExecutor(20) as pool:
        responses = list(pool.map(lambda _: intake_api(rep), range(20)))
    codes = sorted(r.status_code for r in responses)
    assert codes.count(201) == 1 and codes.count(200) == 19, codes
    assert len({r.json()["incident_id"] for r in responses}) == 1
    with session() as db:
        assert db.scalar(select(func.count()).select_from(InboxEvent).where(InboxEvent.event_id == rep["event_id"])) == 1


def test_same_event_id_different_payload_is_409():
    rep = report()
    assert intake_api(rep).status_code == 201
    r = intake_api({**rep, "description": rep["description"] + " (edited)"})
    assert r.status_code == 409 and r.json()["error"] == "event_payload_mismatch"


def test_same_event_id_in_another_source_scope_is_a_different_event():
    rep = report()
    assert intake_api(rep, scope="scope-a").status_code == 201
    assert intake_api(rep, scope="scope-b").status_code == 201


# --- versioned approval -------------------------------------------------------------------------

def test_concurrent_double_approval_creates_one_set_of_actions():
    iid = ready_incident()
    d = incident(iid)
    body = {"expected_revision": d["revision"], "plan_digest": d["review"]["plan_digest"]}
    with ThreadPoolExecutor(10) as pool:
        responses = list(pool.map(lambda _: httpx.post(f"{API}/v1/incidents/{iid}/approve", json=body, headers=REVIEWER, timeout=30), range(10)))
    codes = sorted(r.status_code for r in responses)
    assert codes.count(200) == 1 and codes.count(409) == 9, codes
    assert len(incident(iid)["actions"]) == 2


def test_new_evidence_before_begin_invalidates_approval():
    iid = ready_incident()
    assert approve(iid).status_code == 200
    offer = claim_for(iid, "create_ticket")
    # New telemetry arrives after approval, before n8n calls /begin.
    tel = {"source_id": "telemetry-sim", "available": True, "observed_at": "2026-10-09T10:00:00+00:00", "values": {"battery_pct": 80}}
    assert httpx.post(f"{API}/v1/incidents/{iid}/evidence/telemetry", json=tel, headers=INTERNAL).status_code == 200
    r = begin(offer)
    assert r.status_code == 409, r.text
    d = incident(iid)
    assert d["status"] == "needs_review"
    assert {a["status"] for a in d["actions"]} == {"cancelled"}
    assert tickets_for_key(action(iid, "create_ticket")["action_key"]) == 0


def test_correction_after_approval_cancels_not_started_actions():
    iid = ready_incident()
    assert approve(iid).status_code == 200
    d = incident(iid)
    r = httpx.post(f"{API}/v1/incidents/{iid}/corrections", headers=REVIEWER,
                   json={"expected_revision": d["revision"], "note": "wrong category", "category": "mechanical"})
    assert r.status_code == 200
    d = incident(iid)
    assert d["status"] == "needs_review" and {a["status"] for a in d["actions"]} == {"cancelled"}
    # Re-approval of the new revision produces new action keys.
    assert approve(iid).status_code == 200
    keys = [a["action_key"] for a in incident(iid)["actions"]]
    assert len(keys) == len(set(keys)) == 4


# --- begin / ack protocol -----------------------------------------------------------------------

def test_concurrent_begin_with_the_same_token_authorizes_once():
    iid = ready_incident()
    approve(iid)
    offer = claim_for(iid, "create_ticket")
    with ThreadPoolExecutor(5) as pool:
        codes = sorted(r.status_code for r in pool.map(lambda _: begin(offer), range(5)))
    assert codes.count(200) == 1 and codes.count(409) == 4, codes


def test_stale_callback_after_new_lease_is_rejected():
    iid = ready_incident()
    approve(iid)
    first = claim_for(iid, "create_ticket")
    expire_lease(first["action_id"])  # n8n never called begin within the lease
    with session() as db:
        dispatch.sweep_expired(db, OWNER)
    second = claim_for(iid, "create_ticket")
    assert second["fencing_token"] == first["fencing_token"] + 1
    assert begin(first).status_code == 409                       # old offer
    assert begin(second).status_code == 200
    assert ack(first, "succeeded", "TCK-FAKE").status_code == 409  # old attempt's callback
    assert action(iid, "create_ticket")["status"] == "executing"
    events = [t["event"] for t in timeline(iid)]
    assert "begin_rejected_stale_token" in events and "ack_rejected_stale_token" in events


def test_repeated_ack_with_same_result_is_idempotent():
    iid = ready_incident()
    approve(iid)
    offer = claim_for(iid, "create_ticket")
    begin(offer)
    assert ack(offer, "succeeded", "TCK-X1").json() == {"status": "succeeded", "replayed": False}
    assert ack(offer, "succeeded", "TCK-X1").json() == {"status": "succeeded", "replayed": True}
    assert ack(offer, "succeeded", "TCK-OTHER").status_code == 409


def test_lost_ack_after_remote_creation_is_reconciled_by_key():
    iid = ready_incident()
    approve(iid)
    offer = claim_for(iid, "create_ticket")
    begun = begin(offer).json()
    created = create_ticket_like_n8n(begun)          # the effect happens at the provider...
    assert created.status_code == 201
    expire_lease(offer["action_id"])                 # ...and the ack never arrives (crash)
    with session() as db:
        to_reconcile = dispatch.sweep_expired(db, OWNER)
    assert uuid.UUID(offer["action_id"]) in to_reconcile
    assert action(iid, "create_ticket")["status"] == "uncertain"
    result = dispatch.reconcile(session, uuid.UUID(offer["action_id"]), OWNER)
    assert result["finding"] == "found"
    a = action(iid, "create_ticket")
    assert a["status"] == "succeeded" and a["provider_reference"] == created.json()["ticket_id"]
    assert tickets_for_key(begun["action_key"]) == 1
    # The notification is released only now, after the ticket is known to exist.
    assert action(iid, "notify_team")["available_at"] is not None


def test_late_ack_of_the_expired_attempt_resolves_uncertainty():
    iid = ready_incident()
    approve(iid)
    t = claim_for(iid, "create_ticket")
    begin(t)
    ack(t, "succeeded", "TCK-LATE0")
    n = claim_for(iid, "notify_team")
    begin(n)
    expire_lease(n["action_id"])               # SMTP call outlives its lease
    with session() as db:
        dispatch.sweep_expired(db, OWNER)
    assert action(iid, "notify_team")["status"] == "uncertain"
    assert incident(iid)["status"] == "blocked"
    # Same attempt (same fencing token) finally reports success: accepted as evidence.
    assert ack(n, "succeeded", "<late@repairflow.local>").json()["status"] == "succeeded"
    assert incident(iid)["status"] == "completed"
    assert "action_succeeded_late_ack" in [e["event"] for e in timeline(iid)]


def test_lost_ack_without_remote_creation_is_retried_safely():
    iid = ready_incident()
    approve(iid)
    offer = claim_for(iid, "create_ticket")
    begin(offer)
    expire_lease(offer["action_id"])
    with session() as db:
        dispatch.sweep_expired(db, OWNER)
    assert dispatch.reconcile(session, uuid.UUID(offer["action_id"]), OWNER)["finding"] == "absent"
    assert action(iid, "create_ticket")["status"] == "retry_wait"
    again = claim_for(iid, "create_ticket")
    assert again["fencing_token"] == offer["fencing_token"] + 1


def test_ambiguous_smtp_result_is_uncertain_and_never_auto_retried():
    iid = ready_incident()
    approve(iid)
    t = claim_for(iid, "create_ticket")
    begin(t)
    ack(t, "succeeded", "TCK-AMB")
    n = claim_for(iid, "notify_team")
    begin(n)
    assert ack(n, "ambiguous", detail="connection reset after DATA").json()["status"] == "uncertain"
    assert incident(iid)["status"] == "blocked"
    with session() as db:
        dispatch.sweep_expired(db, OWNER)
        offers = dispatch.claim(db, OWNER, limit=50)
    assert not [o for o in offers if o["action_id"] == n["action_id"]]
    assert action(iid, "notify_team")["status"] == "uncertain"
    assert action(iid, "create_ticket")["status"] == "succeeded"


def test_email_failure_never_recreates_the_ticket():
    iid = ready_incident()
    approve(iid)
    t = claim_for(iid, "create_ticket")
    begun = begin(t).json()
    created = create_ticket_like_n8n(begun).json()
    ack(t, "succeeded", created["ticket_id"])
    n = claim_for(iid, "notify_team")
    begin(n)
    assert ack(n, "permanent", detail="550 mailbox unavailable").json()["status"] == "dead"
    with session() as db:
        dispatch.sweep_expired(db, OWNER)
        dispatch.claim(db, OWNER, limit=50)
    assert action(iid, "create_ticket")["status"] == "succeeded"
    assert tickets_for_key(begun["action_key"]) == 1
    assert incident(iid)["status"] == "blocked"


def test_retryable_failures_back_off_then_die_after_max_attempts():
    iid = ready_incident()
    approve(iid)
    for attempt in range(1, dispatch.MAX_ATTEMPTS + 1):
        with session() as db:
            db.execute(update(Outbox).where(Outbox.action_id == uuid.UUID(action(iid, "create_ticket")["id"]))
                       .values(available_at=func.now()))
            db.commit()
        offer = claim_for(iid, "create_ticket")
        assert begin(offer).json()["attempt"] == attempt
        status = ack(offer, "retryable", http_status=503).json()["status"]
        assert status == ("dead" if attempt == dispatch.MAX_ATTEMPTS else "retry_wait")
    a = action(iid, "create_ticket")
    assert a["status"] == "dead" and a["attempts"] == dispatch.MAX_ATTEMPTS


def test_rate_limit_honours_retry_after():
    iid = ready_incident()
    approve(iid)
    offer = claim_for(iid, "create_ticket")
    begin(offer)
    ack(offer, "rate_limited", http_status=429, retry_after_s=120)
    with session() as db:
        row = db.execute(select(Outbox.available_at, func.now()).where(Outbox.action_id == uuid.UUID(offer["action_id"]))).one()
    assert 110 <= (row[0] - row[1]).total_seconds() <= 121


# --- authorization ------------------------------------------------------------------------------

def test_server_side_authorization():
    iid = ready_incident()
    body = {"expected_revision": 1, "plan_digest": "0" * 64}
    assert httpx.post(f"{API}/v1/incidents/{iid}/approve", json=body).status_code == 401
    assert httpx.post(f"{API}/v1/incidents/{iid}/approve", json=body, headers=OPERATOR).status_code == 403
    assert httpx.post(f"{API}/v1/incidents/{iid}/approve", json=body, headers=INTERNAL).status_code == 403
    assert httpx.post(f"{API}/v1/incidents", json={}, headers={"X-RepairFlow-Internal": "wrong"}).status_code == 401
    assert httpx.post(f"{API}/v1/incidents", json={"source_scope": "x", "report": report()}, headers=REVIEWER).status_code == 403
    fake = {"fencing_token": 1, "dispatch_token": "x"}
    assert httpx.post(f"{API}/v1/actions/{uuid.uuid4()}/begin", json=fake, headers=REVIEWER).status_code == 403


def test_actions_table_invariants_hold():
    with session() as db:
        dup_keys = db.execute(select(Action.action_key).group_by(Action.action_key).having(func.count() > 1)).all()
        negative = db.scalar(select(func.count()).select_from(Outbox).where(Outbox.attempts < 0))
    assert dup_keys == [] and negative == 0
