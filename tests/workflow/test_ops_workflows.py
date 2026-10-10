"""Imported n8n operations workflows (W0 error handler, W5 recovery, W6 digest) and the SSE stream."""
import json
import random
import subprocess
import threading
import time

import httpx
from sqlalchemy import text

from app.db import session
from conftest import ENV, ROOT
from helpers import API, MAILPIT, N8N, OPERATOR, REVIEWER, approve, incident, intake_api, ready_incident, report, wait_for

DISPATCH = {"X-Dispatch-Key": ENV.get("RF_DISPATCH_KEY", "")}


def audit_since(event: str, since_id: int) -> list[dict]:
    with session() as db:
        rows = db.execute(text("SELECT id, payload FROM audit_events WHERE event = :e AND id > :i ORDER BY id"),
                          {"e": event, "i": since_id}).all()
    return [{"id": r[0], **r[1]} for r in rows]


def last_audit_id() -> int:
    with session() as db:
        return db.execute(text("SELECT coalesce(max(id), 0) FROM audit_events")).scalar_one()


def run_digest(period: str) -> None:
    r = httpx.post(f"{N8N}/webhook/rf-digest-run", json={"period_key": period}, headers=DISPATCH, timeout=10)
    assert r.status_code == 200


def test_digest_is_sent_once_per_period_key():
    period = f"20{random.randint(30, 99)}-01-{random.randint(10, 28)}"  # unique per run, valid format
    with session() as db:
        db.execute(text("DELETE FROM digests WHERE period_key = :p"), {"p": period})
        db.commit()
    run_digest(period)
    wait_for(lambda: audit_since("digest_sent", 0) and any(a["period_key"] == period for a in audit_since("digest_sent", 0)),
             30, 1, "first digest sent")
    run_digest(period)
    time.sleep(6)
    found = httpx.get(f"{MAILPIT}/api/v1/search", params={"query": f'subject:"RF-DIGEST-{period}"'}).json()["messages"]
    assert len(found) == 1
    with session() as db:
        status, attempts = db.execute(text("SELECT status, attempts FROM digests WHERE period_key = :p"), {"p": period}).one()
    assert status == "sent" and attempts == 1


def test_error_workflow_reports_a_real_failed_execution():
    since = last_audit_id()
    run_digest("not-a-date")  # the API rejects it with 422; the strict HTTP node fails the execution
    errors = wait_for(lambda: audit_since("n8n_execution_failed", since), 30, 1, "error workflow report")
    assert errors[0]["workflow_name"] == "RF 06 Daily digest" and errors[0]["last_node"] == "Reserve digest"


def test_recovery_schedule_runs_without_manual_trigger():
    since = last_audit_id()
    sweeps = wait_for(lambda: audit_since("recovery_sweep", since), 75, 2, "scheduled W5 sweep")
    assert {"expired_leases", "reconciled", "stalled_intake"} <= set(sweeps[0])


def test_sse_stream_delivers_new_audit_events():
    received, stop = [], threading.Event()

    def listen():
        with httpx.stream("GET", f"{API}/v1/events", headers=REVIEWER, timeout=30) as r:
            for line in r.iter_lines():
                if line.startswith("data: "):
                    received.append(json.loads(line[6:]))
                if stop.is_set():
                    return

    t = threading.Thread(target=listen, daemon=True)
    t.start()
    time.sleep(2)
    iid = ready_incident()
    wait_for(lambda: any(e["incident_id"] == iid and e["event"] == "ready_for_review" for e in received), 15, 0.5, "SSE event")
    stop.set()
    ids = [e["id"] for e in received]
    assert ids == sorted(ids)


def test_sse_requires_a_user_token():
    assert httpx.get(f"{API}/v1/events", timeout=5).status_code == 401


def test_metrics_separate_human_review_time_from_dispatch_time():
    m = httpx.get(f"{API}/v1/metrics", headers=REVIEWER, timeout=10).json()
    assert {"review_time_median_s", "dispatch_time_median_s", "eligible_backlog", "uncertain_actions"} <= set(m)


def test_diagnostics_bundle_is_allow_listed():
    marker = f"diagnostic-marker-{random.randint(10**6, 10**7)}"
    iid = intake_api(report(description=f"Robot stops, {marker} in the report text.")).json()["incident_id"]
    assert httpx.get(f"{API}/v1/ops/diagnostics", headers=OPERATOR, timeout=10).status_code == 403
    d = httpx.get(f"{API}/v1/ops/diagnostics", headers=REVIEWER, timeout=30)
    assert d.status_code == 200
    body = d.text
    assert iid in body and marker not in body                      # ids yes, report text never
    assert not [k for k, v in ENV.items() if v and len(v) >= 8 and v in body]
    assert {"schema", "policy", "rules", "prompt", "llm"} <= set(d.json()["versions"])


def test_correlation_id_reaches_the_ticket_service():
    iid = ready_incident()
    cid = incident(iid)["correlation_id"]
    assert approve(iid).status_code == 200
    wait_for(lambda: incident(iid)["status"] == "completed", 60, 1, "dispatch completed")
    logs = subprocess.run(["docker", "compose", "logs", "ticket-service", "--since", "3m"], capture_output=True,
                          text=True, cwd=ROOT).stdout
    assert any(cid in line and '"ticket_request"' in line for line in logs.splitlines())
