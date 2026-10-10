"""Our handling of LLM provider outputs, against the real PostgreSQL.

The provider here is a local TEST DOUBLE (an HTTP server in this process speaking the OpenAI-compatible
chat API). It exists to produce failures on demand: malformed JSON, invented references, foreign manifest,
extra fields, unavailability, a revision changing during inference. Real-model behaviour is recorded
separately (scripts/sample_llm_runs.py -> artifacts/demo/llm_runs_*.json) and is not claimed here.
"""
import dataclasses
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from sqlalchemy import select

import app.config
from app import incidents
from app.db import session
from app.models import AuditEvent, TriageRun
from helpers import API, INTERNAL, incident, ready_incident


class Provider:
    """Queue of behaviours; each request pops one. Behaviours receive the request's JSON schema."""
    def __init__(self):
        self.queue, self.requests = [], []


PROVIDER = Provider()


def valid_from_schema(schema: dict, **override) -> dict:
    props = schema["properties"]
    ids = props["extracted_facts"]["items"]["properties"]["evidence_refs"]["items"]["enum"]
    out = {
        "incident_revision": props["incident_revision"]["enum"][0],
        "evidence_manifest_id": props["evidence_manifest_id"]["enum"][0],
        "category": "battery", "summary": "Battery shutdown reported; telemetry disagrees with the operator.",
        "extracted_facts": [{"claim": "Operator reports a shutdown.", "evidence_refs": [ids[0]]}],
        "missing_information": [], "contradictions": [], "suggested_next_step": "review",
        "draft_ticket": {"title": "Battery shutdown on robot", "description": f"See [{ids[0]}]."},
    }
    out.update(override)
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        PROVIDER.requests.append(body)
        schema = body["response_format"]["json_schema"]["schema"]
        status, content = PROVIDER.queue.pop(0)(schema) if PROVIDER.queue else (500, None)
        payload = ({"model": "double-model", "system_fingerprint": "fp_double", "usage": {"total_tokens": 1},
                    "choices": [{"finish_reason": "stop", "message": {"content": content}}]}
                   if status == 200 else {"error": {"message": "double failure"}})
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture(scope="module", autouse=True)
def provider_double():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    original = app.config.get_settings()
    app.config._settings = dataclasses.replace(original, llm_base_url=f"http://127.0.0.1:{server.server_port}/v1",
                                               llm_model="double-model", llm_api_key="test-double", llm_timeout_s=5)
    yield
    app.config._settings = original
    server.shutdown()


@pytest.fixture(autouse=True)
def fresh_queue():
    PROVIDER.queue.clear()
    PROVIDER.requests.clear()


def ok(**override):
    return lambda schema: (200, json.dumps(valid_from_schema(schema, **override)))


def triage(iid):
    return incidents.triage(session, uuid.UUID(iid), "pytest")


def runs(iid):
    with session() as db:
        return db.execute(select(TriageRun).where(TriageRun.incident_id == uuid.UUID(iid))
                          .order_by(TriageRun.created_at)).scalars().all()


def events(iid):
    with session() as db:
        return [e.event for e in db.execute(select(AuditEvent).where(
            AuditEvent.incident_id == uuid.UUID(iid)).order_by(AuditEvent.id)).scalars()]


def test_valid_output_is_used_and_fully_recorded():
    iid = ready_incident()
    PROVIDER.queue.append(ok())
    final = triage(iid)
    assert final.engine == "llm" and final.valid
    assert final.model == "double-model" and final.model_digest == "fp_double"
    assert final.context["schema"]["additionalProperties"] is False and "untrusted" in final.context["system"]
    assert final.params["temperature"] == 0 and final.params["attempt"] == 1
    assert incident(iid)["review"]["provenance"] == "llm"


def test_provider_unavailable_falls_back_to_rules():
    iid = ready_incident()
    PROVIDER.queue.append(lambda s: (500, None))
    final = triage(iid)
    assert final.engine == "rules" and final.valid
    llm_runs = [r for r in runs(iid) if r.engine == "llm"]
    assert len(llm_runs) == 1 and "provider HTTP 500" in llm_runs[-1].errors[0]  # no repair for unavailability
    assert "llm_fallback_to_rules" in events(iid)
    assert incident(iid)["status"] == "needs_review"


def test_malformed_json_gets_one_repair_then_falls_back():
    iid = ready_incident()
    PROVIDER.queue += [lambda s: (200, "{not json"), lambda s: (200, '{"category": "battery"}')]
    final = triage(iid)
    assert final.engine == "rules"
    assert [r.valid for r in runs(iid) if r.engine == "llm"][-2:] == [False, False]
    assert len(PROVIDER.requests) == 2  # bounded: exactly one repair
    assert PROVIDER.requests[1]["messages"][-1]["content"].startswith("Your output failed validation")


def test_repair_can_succeed():
    iid = ready_incident()
    PROVIDER.queue += [lambda s: (200, "{not json"), ok()]
    final = triage(iid)
    assert final.engine == "llm" and final.valid and final.params["attempt"] == 2


def test_invented_reference_is_rejected():
    iid = ready_incident()
    bad = [{"claim": "Pack is defective", "evidence_refs": ["man-doesnotexist"]}]
    PROVIDER.queue += [ok(extracted_facts=bad), ok(extracted_facts=bad)]
    final = triage(iid)
    assert final.engine == "rules"
    assert any("unknown evidence ref 'man-doesnotexist'" in e for r in runs(iid) if r.engine == "llm" for e in r.errors)


def test_foreign_manifest_and_wrong_revision_are_rejected():
    iid = ready_incident()
    PROVIDER.queue += [ok(evidence_manifest_id="mf-other", incident_revision=99)] * 2
    triage(iid)
    errors = [e for r in runs(iid) if r.engine == "llm" for e in r.errors]
    assert any("evidence_manifest_id" in e for e in errors) and any("incident_revision 99" in e for e in errors)


def test_out_of_contract_fields_cannot_carry_actions():
    iid = ready_incident()
    smuggled = lambda s: (200, json.dumps({**valid_from_schema(s), "actions": [{"type": "send_email", "to": "x@example.com"}],
                                           "priority": "P1"}))
    PROVIDER.queue += [smuggled, smuggled]
    final = triage(iid)
    assert final.engine == "rules"
    assert any("extra" in e.lower() or "forbidden" in e.lower() for r in runs(iid) if r.engine == "llm" for e in r.errors)
    plan = incident(iid)["review"]["plan"]
    assert {a["type"] for a in plan} == {"create_ticket", "notify_team"}
    assert plan[1]["args"]["to"].endswith("@repairflow.local")


def test_revision_change_during_inference_discards_the_output():
    iid = ready_incident()

    def change_then_answer(schema):
        tel = {"source_id": "telemetry-sim", "available": True, "observed_at": "2026-10-09T12:00:00+00:00",
               "values": {"battery_pct": 50}}
        assert httpx.post(f"{API}/v1/incidents/{iid}/evidence/telemetry", json=tel, headers=INTERNAL).status_code == 200
        return 200, json.dumps(valid_from_schema(schema))

    PROVIDER.queue.append(change_then_answer)
    final = triage(iid)
    stale = [r for r in runs(iid) if r.engine == "llm"][-1]
    assert not stale.valid and any("revision changed during inference" in e for e in stale.errors)
    assert final.engine == "rules" and final.incident_revision == incident(iid)["revision"]


def test_rules_engine_is_used_when_requested_explicitly():
    iid = ready_incident()
    final = incidents.triage(session, uuid.UUID(iid), "pytest", engine="rules")
    assert final.engine == "rules" and PROVIDER.requests == []


def test_next_step_is_forced_to_clarify_when_a_contradiction_is_reported():
    iid = ready_incident()

    def contradiction_but_ticket(schema):
        ids = schema["properties"]["extracted_facts"]["items"]["properties"]["evidence_refs"]["items"]["enum"]
        return 200, json.dumps(valid_from_schema(schema, suggested_next_step="prepare_ticket", contradictions=[
            {"claim_a": "Operator: battery full", "claim_b": "Telemetry: 12 %", "evidence_refs": ids[:2]}]))

    PROVIDER.queue.append(contradiction_but_ticket)
    final = triage(iid)
    assert final.engine == "llm" and final.valid
    assert final.proposal["suggested_next_step"] == "clarify"
    assert json.loads(final.raw_output)["suggested_next_step"] == "prepare_ticket"   # model output kept as is
    assert final.params["adjustments"] and "contradiction reported" in final.params["adjustments"][0]
    assert incident(iid)["review"]["adjustments"] == final.params["adjustments"]
