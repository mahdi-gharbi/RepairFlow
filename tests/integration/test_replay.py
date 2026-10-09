"""Incident Replay Lab against the real database (sandbox simulator, no connector calls)."""
import httpx
import pytest
from sqlalchemy import func, select

from app.db import session
from app.models import Action
from helpers import API, INTERNAL, OPERATOR, REVIEWER, approve, ready_incident


@pytest.fixture(scope="module")
def source():
    """Approved incident: battery contradiction, telemetry + manual evidence, rules proposal."""
    iid = ready_incident()
    httpx.post(f"{API}/v1/incidents/{iid}/evidence/manuals", headers=INTERNAL, timeout=30)
    httpx.post(f"{API}/v1/incidents/{iid}/triage?engine=rules", headers=INTERNAL, timeout=30)
    assert approve(iid).status_code == 200
    return iid


def replay(iid, scenario, mode="frozen", seed=7):
    r = httpx.post(f"{API}/v1/replays", headers=REVIEWER, timeout=60,
                   json={"incident_id": iid, "scenario": scenario, "mode": mode, "seed": seed})
    assert r.status_code == 200, r.text
    return r.json()


def test_replays_never_create_real_actions(source):
    for scenario in ("baseline", "ticket_503_twice", "crash_after_remote_creation", "email_accepted_response_lost"):
        replay(source, scenario)
    with session() as db:
        keys = db.scalars(select(Action.action_key).where(Action.incident_id == source)).all()
        replay_keys = db.scalar(select(func.count()).select_from(Action).where(Action.action_key.like("replay:%")))
    # The source keeps exactly the two actions of its real approval (the worker may dispatch them); none added.
    assert len(keys) == 2 and all(k.startswith(f"rf:{source}:") for k in keys)
    assert replay_keys == 0


def test_baseline_reproduces_the_source_decision(source):
    r = replay(source, "baseline")
    assert r["comparison"] == []
    assert r["replay"]["approval_still_valid"] is True
    assert all(a["action_key"].startswith(f"replay:{r['id']}:") for a in r["replay"]["actions"])


def test_frozen_mode_is_deterministic_for_a_seed(source):
    a, b = replay(source, "ticket_503_twice", seed=11), replay(source, "ticket_503_twice", seed=11)
    strip = lambda run: [{k: v for k, v in s.items() if k != "source_incident"} for s in run["trace"]]
    assert strip(a) == strip(b)
    assert a["replay"]["actions"][0]["attempts"] == 3


def test_crash_after_remote_creation_reconciles_without_duplicate(source):
    r = replay(source, "crash_after_remote_creation")
    ticket = r["replay"]["actions"][0]
    assert ticket["status"] == "succeeded" and ticket["attempts"] == 1 and r["replay"]["tickets_created"] == 1
    events = [s["event"] for s in r["trace"]]
    assert events.index("execution_lease_expired") < events.index("reconciled_found")


def test_new_manual_after_approval_requires_review(source):
    r = replay(source, "new_manual_after_approval")
    assert r["replay"]["approval_still_valid"] is False
    assert any(c["field"] == "approval" for c in r["comparison"])
    assert "actions_cancelled_superseded" in [s["event"] for s in r["trace"]]


def test_malicious_instruction_does_not_change_authorized_actions(source):
    r = replay(source, "malicious_instruction")
    assert r["replay"]["authorized_actions"] == r["source"]["authorized_actions"]
    assert all(a.get("to", "").endswith("@repairflow.local") for a in r["replay"]["authorized_actions"] if a["to"])


def test_lost_smtp_response_needs_a_human(source):
    r = replay(source, "email_accepted_response_lost")
    notify = r["replay"]["actions"][1]
    assert notify["status"] == "uncertain" and r["replay"]["human_decision_needed"] is True
    assert r["replay"]["tickets_created"] == 1


def test_alternative_policy_blocks_actions_while_contradiction_is_open(source):
    r = replay(source, "policy_clarify_on_contradiction")
    assert r["source"]["contradictions"] >= 1
    assert r["replay"]["authorized_actions"] == [] and r["replay"]["tickets_created"] == 0


def test_duplicate_deliveries_and_missing_telemetry(source):
    d = replay(source, "duplicate_delivery_x5")["replay"]["deliveries"]
    assert d == {"created": 1, "replayed": 4, "conflict": 1}
    t = replay(source, "telemetry_unavailable")
    assert t["replay"]["contradictions"] == 0 and t["replay"]["approval_still_valid"] is False


def test_invented_citation_in_recorded_output_falls_back(source):
    r = replay(source, "llm_invented_citation")
    assert r["replay"]["provenance"] == "rules"


def test_operator_cannot_run_a_replay_and_bad_scenario_is_rejected(source):
    body = {"incident_id": source, "scenario": "baseline"}
    assert httpx.post(f"{API}/v1/replays", headers=OPERATOR, json=body).status_code == 403
    assert httpx.post(f"{API}/v1/replays", headers=REVIEWER, json={**body, "scenario": "nope"}).status_code == 422
    listed = httpx.get(f"{API}/v1/replays", params={"incident_id": source}, headers=OPERATOR).json()
    assert listed and httpx.get(f"{API}/v1/replays/{listed[0]['id']}/comparison", headers=OPERATOR).status_code == 200
