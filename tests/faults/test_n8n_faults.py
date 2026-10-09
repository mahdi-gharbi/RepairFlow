"""Fault injection through the real chain: worker -> n8n dispatch workflows -> simulators -> API ack.

Nothing here calls begin/ack directly: outcomes are produced by the imported n8n workflows.
"""
import pytest
from sqlalchemy import text

from app.db import session
from helpers import (
    TELEMETRY, TICKETS, action, approve, clear_faults, compose, incident, inject, intake_api, intake_n8n, mails_with,
    ready_incident, report, tickets_for_incident, timeline, wait_for,
)


@pytest.fixture(autouse=True)
def clean_faults():
    compose("start", "worker", "n8n", "mailpit")
    clear_faults()
    yield
    clear_faults()


def events(iid):
    return [t["event"] for t in timeline(iid)]


def final(iid, timeout=90):
    return wait_for(lambda: incident(iid)["status"] in ("completed", "blocked") and incident(iid), timeout, 1, f"{iid} settled")


def test_ticket_service_503_twice_then_success():
    inject(TICKETS, mode="fail_before", status=503, count=2)
    iid = ready_incident()
    assert approve(iid).status_code == 200
    d = final(iid)
    t = action(iid, "create_ticket")
    assert d["status"] == "completed", d
    assert t["attempts"] == 3 and t["status"] == "succeeded"
    assert len(tickets_for_incident(iid)) == 1
    assert events(iid).count("action_retry_scheduled") == 2


def test_rate_limit_retry_after_is_honoured():
    inject(TICKETS, mode="fail_before", status=429, count=1, retry_after_s=3)
    iid = ready_incident()
    approve(iid)
    assert final(iid)["status"] == "completed"
    retry = next(t for t in timeline(iid) if t["event"] == "action_retry_scheduled")
    assert retry["payload"]["retry_in_s"] == 3 and retry["payload"]["http_status"] == 429


def test_timeout_after_remote_creation_is_reconciled_without_duplicate():
    # The ticket is created, but the answer comes after n8n's 10 s timeout: outcome unknown.
    inject(TICKETS, mode="delay_after", delay_s=15, count=1)
    iid = ready_incident()
    approve(iid)
    d = final(iid, timeout=120)
    assert d["status"] == "completed", d
    ev = events(iid)
    assert "action_outcome_unknown" in ev and "reconciled_found" in ev
    tickets = tickets_for_incident(iid)
    assert len(tickets) == 1 and action(iid, "create_ticket")["provider_reference"] == tickets[0]["ticket_id"]


def test_5xx_after_processing_is_ambiguous_then_reconciled():
    inject(TICKETS, mode="fail_after", status=502, count=1)
    iid = ready_incident()
    approve(iid)
    assert final(iid)["status"] == "completed"
    assert "reconciled_found" in events(iid)
    assert len(tickets_for_incident(iid)) == 1


def test_n8n_down_before_reception_then_recovered():
    compose("stop", "n8n")
    try:
        iid = ready_incident()
        approve(iid)
        wait_for(lambda: "dispatch_not_received" in events(iid), 30, 1, "worker noticed n8n is down")
        assert action(iid, "create_ticket")["status"] in ("retry_wait", "offered", "pending")
    finally:
        compose("start", "n8n")
    d = final(iid, timeout=180)
    assert d["status"] == "completed"
    assert len(tickets_for_incident(iid)) == 1


def test_smtp_outage_delays_email_without_recreating_ticket():
    # Observed behaviour (n8n 2.42.6): with Mailpit stopped, the SMTP node does not fail fast; nodemailer
    # keeps connecting (no configurable timeout). The attempt must stay within its lease or become
    # uncertain -- never a second ticket, never a duplicate email.
    compose("stop", "mailpit")
    try:
        iid = ready_incident()
        approve(iid)
        wait_for(lambda: action(iid, "notify_team")["status"] in ("executing", "retry_wait", "uncertain"), 60, 1, "email attempt started")
        assert action(iid, "create_ticket")["status"] == "succeeded"
    finally:
        compose("start", "mailpit")
    d = final(iid, timeout=240)
    n = action(iid, "notify_team")
    assert d["status"] == "completed", (d["status"], n)
    assert len(tickets_for_incident(iid)) == 1
    marker = "RF-" + n["id"].replace("-", "")[:12]
    assert len(mails_with(marker)) == 1


def test_telemetry_unavailable_is_recorded_as_missing_not_invented():
    inject(TELEMETRY, mode="unavailable", status=503, count=1)
    r = intake_n8n(report(equipment="robot-02", description="Bruit de grincement sur la roue arrière gauche."))
    assert r.status_code == 201, r.text
    assert r.json()["telemetry_available"] is False
    d = incident(r.json()["incident_id"])
    tel = [e for e in d["evidence"] if e["kind"] == "telemetry"]
    assert len(tel) == 1 and tel[0]["content"]["available"] is False and tel[0]["content"]["values"] == {}
    p = d["review"]["proposal"]
    assert p["missing_information"]                      # whichever engine produced it
    if d["review"]["provenance"] == "rules":
        assert any("unavailable" in m for m in p["missing_information"])


def test_stale_telemetry_is_flagged():
    r = intake_n8n(report(equipment="robot-03", description="Le capteur lidar ne détecte plus les obstacles."))
    assert r.status_code == 201, r.text
    d = incident(r.json()["incident_id"])
    tel = next(e for e in d["evidence"] if e["kind"] == "telemetry")
    assert tel["content"]["available"] is True           # stale data is kept, dated, and not hidden
    p = d["review"]["proposal"]
    if d["review"]["provenance"] == "rules":
        assert any("stale" in m for m in p["missing_information"])


def test_pending_actions_survive_api_worker_and_n8n_restarts():
    compose("stop", "worker")
    iid = ready_incident()
    assert approve(iid).status_code == 200            # committed to PostgreSQL, nobody dispatching
    compose("restart", "api", "n8n")
    compose("start", "worker")
    d = final(iid, timeout=180)
    assert d["status"] == "completed"
    assert len(tickets_for_incident(iid)) == 1


def test_intake_interrupted_before_triage_is_recovered_by_worker():
    r = intake_api(report(equipment="robot-01", description="Robot arm vibrates strongly when lifting."))
    iid = r.json()["incident_id"]                    # incident stored; no telemetry, no triage
    with session() as db:  # simulate time passing since the interrupted intake
        db.execute(text("UPDATE incidents SET updated_at = now() - interval '10 minutes' WHERE id = :i"), {"i": iid})
        db.commit()
    d = wait_for(lambda: incident(iid)["status"] == "needs_review" and incident(iid), 60, 1, "worker recovery")
    assert any("No telemetry" in m for m in d["review"]["proposal"]["missing_information"])
