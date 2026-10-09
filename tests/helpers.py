"""Helpers for tests that run against the live compose stack (PostgreSQL, API, n8n, simulators)."""
import os
import subprocess
import time
import uuid
from datetime import datetime, timezone

import httpx

from conftest import ENV, ROOT

API = "http://127.0.0.1:18000"
N8N = "http://127.0.0.1:15678"
TICKETS = "http://127.0.0.1:18081"
TELEMETRY = "http://127.0.0.1:18082"
MAILPIT = "http://127.0.0.1:18025"

INTERNAL = {"X-RepairFlow-Internal": ENV.get("RF_INTERNAL_KEY", "")}
REVIEWER = {"Authorization": f"Bearer {ENV.get('RF_REVIEWER_TOKEN', '')}"}
OPERATOR = {"Authorization": f"Bearer {ENV.get('RF_OPERATOR_TOKEN', '')}"}
FAULTS = {"X-Faults-Key": ENV.get("SIM_FAULTS_KEY", "")}
TICKET_KEY = {"X-Api-Key": ENV.get("TICKET_API_KEY", "")}


def stack_up() -> bool:
    try:
        return httpx.get(f"{API}/healthz", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


def compose(*args: str) -> None:
    subprocess.run(["docker", "compose", *args], cwd=ROOT, check=True, capture_output=True)


def report(equipment="robot-07", description="Le robot s'arrête après trois minutes, batterie annoncée pleine.", **kw):
    return {"event_id": kw.pop("event_id", f"evt-test-{uuid.uuid4().hex[:12]}"), "equipment_id": equipment,
            "reported_at": kw.pop("reported_at", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
            "language": "fr", "description": description, **kw}


def intake_api(rep: dict, scope="test-direct") -> httpx.Response:
    return httpx.post(f"{API}/v1/incidents", json={"source_scope": scope, "report": rep}, headers=INTERNAL, timeout=30)


def intake_n8n(rep: dict) -> httpx.Response:
    return httpx.post(f"{N8N}/webhook/rf-intake", json=rep, headers={"X-Intake-Key": ENV["RF_INTAKE_KEY"]}, timeout=30)


def ready_incident(equipment="robot-07", battery=12) -> str:
    """Incident in needs_review through the API (no n8n), with telemetry evidence and rules triage."""
    r = intake_api(report(equipment))
    assert r.status_code == 201, r.text
    iid = r.json()["incident_id"]
    tel = {"source_id": "telemetry-sim", "available": True, "observed_at": datetime.now(timezone.utc).isoformat(),
           "values": {"battery_pct": battery, "network_online": True}}
    assert httpx.post(f"{API}/v1/incidents/{iid}/evidence/telemetry", json=tel, headers=INTERNAL).status_code == 200
    assert httpx.post(f"{API}/v1/incidents/{iid}/triage", headers=INTERNAL).status_code == 200
    return iid


def incident(iid: str) -> dict:
    r = httpx.get(f"{API}/v1/incidents/{iid}", headers=REVIEWER, timeout=10)
    r.raise_for_status()
    return r.json()


def timeline(iid: str) -> list[dict]:
    return httpx.get(f"{API}/v1/incidents/{iid}/timeline", headers=REVIEWER, timeout=10).json()


def approve(iid: str) -> httpx.Response:
    d = incident(iid)
    return httpx.post(f"{API}/v1/incidents/{iid}/approve", headers=REVIEWER, timeout=30,
                      json={"expected_revision": d["revision"], "plan_digest": d["review"]["plan_digest"]})


def action(iid: str, kind: str) -> dict:
    return next(a for a in incident(iid)["actions"] if a["type"] == kind)


def wait_for(predicate, timeout=60.0, interval=0.5, what="condition"):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}; last={last!r}")


def tickets_for_key(key: str) -> int:
    r = httpx.get(f"{TICKETS}/tickets?limit=500", headers=TICKET_KEY, timeout=10)
    return sum(1 for t in r.json() if t["idempotency_key"] == key)


def tickets_for_incident(iid: str) -> list[dict]:
    r = httpx.get(f"{TICKETS}/tickets?limit=500", headers=TICKET_KEY, timeout=10)
    return [t for t in r.json() if t["external_reference"] == iid]


def mails_with(marker: str) -> list[dict]:
    r = httpx.get(f"{MAILPIT}/api/v1/search", params={"query": f'subject:"{marker}"'}, timeout=10)
    return r.json().get("messages") or []


def inject(base: str, **fault) -> None:
    httpx.post(f"{base}/_faults", json=fault, headers=FAULTS, timeout=5).raise_for_status()


def clear_faults() -> None:
    for base in (TICKETS, TELEMETRY):
        httpx.delete(f"{base}/_faults", headers=FAULTS, timeout=5)
