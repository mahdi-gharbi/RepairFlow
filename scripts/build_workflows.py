"""Generates the source JSON of the n8n workflows (workflows/src/*.json).

The committed reference is the *export* produced by n8n after import (workflows/*.json, see
scripts/provision_n8n.sh). Node typeVersions below were read from the installed n8n 2.42.6
(n8n-nodes-base/dist/nodes/*): Webhook 2, HTTP Request 4.2, Code 2, If 2.2, Respond to Webhook 1.1,
Send Email 2.1, Schedule Trigger 1.2, Error Trigger 1. Credentials are referenced by id/name only; their secrets never appear here.
"""
import json
import pathlib
import uuid

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "workflows" / "src"
NS = uuid.UUID("6f1c7a52-55a4-4d7e-9d0a-2f6a8f3f0c11")
API = "http://api:8000"

CRED = {
    "intake": {"httpHeaderAuth": {"id": "rfCredIntakeKey1", "name": "RF Intake Webhook Key"}},
    "dispatch": {"httpHeaderAuth": {"id": "rfCredDispatchK1", "name": "RF Dispatch Webhook Key"}},
    "internal": {"httpHeaderAuth": {"id": "rfCredInternalK1", "name": "RF Internal API"}},
    "ticket": {"httpHeaderAuth": {"id": "rfCredTicketKey1", "name": "RF Ticket API"}},
    "smtp": {"smtp": {"id": "rfCredMailpitSm1", "name": "RF Mailpit SMTP"}},
    "form": {"httpBasicAuth": {"id": "rfCredOperForm01", "name": "RF Operator Form"}},
}


def nid(wf: str, name: str) -> str:
    return str(uuid.uuid5(NS, f"{wf}/{name}"))


def webhook(wf, name, path, cred, respond, pos):
    return {"id": nid(wf, name), "name": name, "type": "n8n-nodes-base.webhook", "typeVersion": 2, "position": pos,
            "webhookId": nid(wf, "webhook:" + path), "credentials": CRED[cred],
            "parameters": {"httpMethod": "POST", "path": path, "authentication": "headerAuth",
                           "responseMode": respond, "options": {}}}


def http(wf, name, method, url, pos, *, cred="internal", body=None, headers=None, timeout=10000, on_error=True,
         strict=False):
    # strict=True: a non-2xx fails the node (and the execution, which then reaches the error workflow).
    # Otherwise status codes are classified explicitly downstream; no implicit connector retries.
    response = {} if strict else {"response": {"response": {"fullResponse": True, "neverError": True}}}
    params = {"method": method, "url": url, "authentication": "genericCredentialType",
              "genericAuthType": "httpHeaderAuth", "options": {**response, "timeout": timeout}}
    if body is not None:
        params.update(sendBody=True, specifyBody="json", jsonBody=body)
    if headers:
        params.update(sendHeaders=True, headerParameters={"parameters": [{"name": k, "value": v} for k, v in headers.items()]})
    node = {"id": nid(wf, name), "name": name, "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
            "position": pos, "parameters": params, "credentials": CRED[cred], "retryOnFail": False}
    if on_error and not strict:
        node["onError"] = "continueRegularOutput"  # network errors become data for the classifier
    return node


def code(wf, name, js, pos):
    return {"id": nid(wf, name), "name": name, "type": "n8n-nodes-base.code", "typeVersion": 2, "position": pos,
            "parameters": {"jsCode": js.strip()}}


def if_status(wf, name, expr, value, pos):
    return {"id": nid(wf, name), "name": name, "type": "n8n-nodes-base.if", "typeVersion": 2.2, "position": pos,
            "parameters": {"conditions": {
                "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 2},
                "conditions": [{"id": nid(wf, name + ":c"), "leftValue": expr, "rightValue": value,
                                "operator": {"type": "number", "operation": "equals"}}],
                "combinator": "and"}, "options": {}}}


def respond(wf, name, code_expr, body_expr, pos):
    return {"id": nid(wf, name), "name": name, "type": "n8n-nodes-base.respondToWebhook", "typeVersion": 1.1,
            "position": pos, "parameters": {"respondWith": "json", "responseBody": body_expr,
                                            "options": {"responseCode": code_expr}}}


def link(*pairs):
    conns: dict = {}
    for src, dst, out in pairs:
        outs = conns.setdefault(src, {"main": []})["main"]
        while len(outs) <= out:
            outs.append([])
        outs[out].append({"node": dst, "type": "main", "index": 0})
    return conns


ERROR_WORKFLOW_ID = "RFw00ErrorHndl1x"


def workflow(wid, name, nodes, connections):
    settings = {"executionOrder": "v1", "saveDataSuccessExecution": "all", "saveDataErrorExecution": "all",
                "saveManualExecutions": True, "callerPolicy": "workflowsFromSameOwner"}
    if wid != ERROR_WORKFLOW_ID:
        settings["errorWorkflow"] = ERROR_WORKFLOW_ID  # failed executions are reported to the API audit log
    return {"id": wid, "name": name, "nodes": nodes, "connections": connections, "active": False,
            "settings": settings, "pinData": {}, "tags": []}


def schedule(wf, name, rule, pos):
    return {"id": nid(wf, name), "name": name, "type": "n8n-nodes-base.scheduleTrigger", "typeVersion": 1.2,
            "position": pos, "parameters": {"rule": {"interval": [rule]}}}


# --- W1 intake / enrichment ---------------------------------------------------------------------

# Correlation id propagated as a header on every downstream call (logged by the API and the ticket service).
CORR_W1 = {"X-Correlation-Id": "={{ $('Create incident').first().json.body.correlation_id }}"}
CORR_DISPATCH = {"X-Correlation-Id": "={{ $('Dispatch webhook').first().json.body.correlation_id }}"}

W1 = "01_intake"
TELEMETRY_EVIDENCE_JS = r"""
// Telemetry is optional evidence: an unavailable source is recorded, never invented.
const r = $input.first().json;
if (r.statusCode === 200 && r.body && r.body.observed_at) {
  return [{ json: { source_id: 'telemetry-sim', available: true, observed_at: r.body.observed_at, values: r.body.values || {} } }];
}
const reason = r.statusCode ? `HTTP ${r.statusCode}` : JSON.stringify(r.error || r).slice(0, 200);
return [{ json: { source_id: 'telemetry-sim', available: false, values: {}, error: reason } }];
"""
w1 = workflow("RFw01Intake00001", "RF 01 Intake and enrichment", [
    webhook(W1, "Intake webhook", "rf-intake", "intake", "responseNode", [0, 300]),
    http(W1, "Create incident", "POST", f"{API}/v1/incidents", [220, 300],
         body="={{ JSON.stringify({ source_scope: 'n8n-intake-webhook', report: $json.body }) }}", on_error=False),
    if_status(W1, "New incident?", "={{ $json.statusCode }}", 201, [440, 300]),
    respond(W1, "Respond replay or rejection", "={{ $json.statusCode }}", "={{ JSON.stringify($json.body) }}", [660, 480]),
    http(W1, "Fetch telemetry", "GET",
         "=http://telemetry-sim:8000/telemetry/{{ encodeURIComponent($('Create incident').first().json.body.equipment_id) }}",
         [660, 200], timeout=5000, headers=CORR_W1),
    code(W1, "Build telemetry evidence", TELEMETRY_EVIDENCE_JS, [880, 200]),
    http(W1, "Record telemetry", "POST",
         "=" + API + "/v1/incidents/{{ $('Create incident').first().json.body.incident_id }}/evidence/telemetry",
         [1100, 200], body="={{ JSON.stringify($json) }}", headers=CORR_W1),
    http(W1, "Retrieve manuals", "POST",
         "=" + API + "/v1/incidents/{{ $('Create incident').first().json.body.incident_id }}/evidence/manuals", [1210, 200], headers=CORR_W1),
    http(W1, "Run triage", "POST",
         "=" + API + "/v1/incidents/{{ $('Create incident').first().json.body.incident_id }}/triage", [1430, 200], timeout=90000, headers=CORR_W1),
    respond(W1, "Respond created", 201,
            "={{ JSON.stringify({ incident_id: $('Create incident').first().json.body.incident_id, "
            "correlation_id: $('Create incident').first().json.body.correlation_id, replayed: false, "
            "telemetry_available: $('Build telemetry evidence').first().json.available, "
            "triage_http_status: $json.statusCode, triage: $json.body }) }}", [1650, 200]),
], link(("Intake webhook", "Create incident", 0), ("Create incident", "New incident?", 0),
        ("New incident?", "Fetch telemetry", 0), ("New incident?", "Respond replay or rejection", 1),
        ("Fetch telemetry", "Build telemetry evidence", 0), ("Build telemetry evidence", "Record telemetry", 0),
        ("Record telemetry", "Retrieve manuals", 0), ("Retrieve manuals", "Run triage", 0), ("Run triage", "Respond created", 0)))

# --- W3 dispatch: ticket ------------------------------------------------------------------------

W3 = "03_dispatch_ticket"
CLASSIFY_TICKET_JS = r"""
// Ticket-service contract: 503/429 = rejected before processing (no effect); 409 = key reused
// with another payload; other 5xx or timeout = effect unknown (reconciled by key, never assumed).
const hook = $('Dispatch webhook').first().json.body;
const r = $input.first().json;
const s = r.statusCode;
let outcome, ref = null, retryAfter = null, detail = null;
if (s === 200 || s === 201) { outcome = 'succeeded'; ref = r.body.ticket_id; }
else if (s === 429) {
  outcome = 'rate_limited';
  const ra = Number((r.headers || {})['retry-after']);
  retryAfter = Number.isFinite(ra) ? ra : null;
}
else if (s === 503) { outcome = 'retryable'; }
else if (s >= 400 && s < 500) { outcome = 'permanent'; }
else if (s) { outcome = 'ambiguous'; }
else {
  detail = JSON.stringify(r.error || r).slice(0, 300);
  outcome = /ECONNREFUSED|ENOTFOUND|EAI_AGAIN/.test(detail) ? 'retryable' : 'ambiguous';
}
if (!detail && outcome !== 'succeeded') detail = JSON.stringify(r.body ?? null).slice(0, 300);
return [{ json: { action_id: hook.action_id, ack: { fencing_token: hook.fencing_token, dispatch_token: hook.dispatch_token,
  outcome, provider_reference: ref, http_status: s || null, retry_after_s: retryAfter, detail } } }];
"""
BEGIN_BODY = "={{ JSON.stringify({ fencing_token: $json.body.fencing_token, dispatch_token: $json.body.dispatch_token }) }}"
TICKET_BODY = ("={{ JSON.stringify({ title: $json.body.args.title, description: $json.body.args.description, "
               "priority: $json.body.args.priority, equipment_id: $json.body.args.equipment_id, site: $json.body.args.site, "
               "category: $json.body.args.category, external_reference: $json.body.args.incident_id }) }}")
w3 = workflow("RFw03DispTicket1", "RF 03 Dispatch ticket", [
    webhook(W3, "Dispatch webhook", "rf-dispatch-ticket", "dispatch", "onReceived", [0, 300]),
    http(W3, "Begin", "POST", "=" + API + "/v1/actions/{{ $json.body.action_id }}/begin", [220, 300], body=BEGIN_BODY,
         on_error=False, headers=CORR_DISPATCH),
    if_status(W3, "Begin authorized?", "={{ $json.statusCode }}", 200, [440, 300]),
    http(W3, "Create ticket", "POST", "http://ticket-service:8000/tickets", [660, 200], cred="ticket", body=TICKET_BODY,
         headers={"Idempotency-Key": "={{ $json.body.action_key }}", **CORR_DISPATCH}, timeout=10000),
    code(W3, "Classify ticket result", CLASSIFY_TICKET_JS, [880, 200]),
    http(W3, "Ack", "POST", "=" + API + "/v1/actions/{{ $json.action_id }}/ack", [1100, 200],
         body="={{ JSON.stringify($json.ack) }}", headers=CORR_DISPATCH),
], link(("Dispatch webhook", "Begin", 0), ("Begin", "Begin authorized?", 0), ("Begin authorized?", "Create ticket", 0),
        ("Create ticket", "Classify ticket result", 0), ("Classify ticket result", "Ack", 0)))

# --- W4 dispatch: notification ------------------------------------------------------------------

W4 = "04_dispatch_notification"
CLASSIFY_EMAIL_JS = r"""
// SMTP has no idempotency key. Connection refused = nothing sent (retryable); auth/recipient
// rejection = permanent; anything else after the transaction may have started = ambiguous.
const hook = $('Dispatch webhook').first().json.body;
const r = $input.first().json;
let outcome, ref = null, detail = null;
if (r.messageId && Array.isArray(r.accepted) && r.accepted.length > 0) { outcome = 'succeeded'; ref = r.messageId; }
else {
  detail = (typeof r.error === 'string' ? r.error : JSON.stringify(r.error || r)).slice(0, 300);
  if (/ECONNREFUSED|ENOTFOUND|EAI_AGAIN/.test(detail)) outcome = 'retryable';
  else if (/EAUTH|Invalid login|EENVELOPE|\b55[0-4]\b/i.test(detail)) outcome = 'permanent';
  else outcome = 'ambiguous';
}
return [{ json: { action_id: hook.action_id, ack: { fencing_token: hook.fencing_token, dispatch_token: hook.dispatch_token,
  outcome, provider_reference: ref, http_status: null, retry_after_s: null, detail } } }];
"""
w4 = workflow("RFw04DispNotify1", "RF 04 Dispatch notification", [
    webhook(W4, "Dispatch webhook", "rf-dispatch-notification", "dispatch", "onReceived", [0, 300]),
    http(W4, "Begin", "POST", "=" + API + "/v1/actions/{{ $json.body.action_id }}/begin", [220, 300], body=BEGIN_BODY,
         on_error=False, headers=CORR_DISPATCH),
    if_status(W4, "Begin authorized?", "={{ $json.statusCode }}", 200, [440, 300]),
    {"id": nid(W4, "Send email"), "name": "Send email", "type": "n8n-nodes-base.emailSend", "typeVersion": 2.1,
     "position": [660, 200], "credentials": CRED["smtp"], "onError": "continueRegularOutput", "retryOnFail": False,
     "parameters": {
         "fromEmail": "RepairFlow <repairflow@repairflow.local>",
         # Recipient comes from the approved, frozen arguments (team config), never from report text.
         "toEmail": "={{ $json.body.args.to }}",
         "subject": "={{ $json.body.args.subject }} [{{ $json.body.context.message_marker }}]",
         "emailFormat": "text",
         "text": ("=A maintenance ticket was created after human approval.\n\n"
                  "Ticket: {{ $json.body.context.ticket_reference }}\n"
                  "Equipment: {{ $json.body.args.equipment_id }}\n"
                  "Incident: {{ $json.body.args.incident_id }}\n"
                  "Action: {{ $json.body.action_id }} (attempt {{ $json.body.attempt }})\n"),
         "options": {"appendAttribution": False}}},
    code(W4, "Classify email result", CLASSIFY_EMAIL_JS, [880, 200]),
    http(W4, "Ack", "POST", "=" + API + "/v1/actions/{{ $json.action_id }}/ack", [1100, 200],
         body="={{ JSON.stringify($json.ack) }}", headers=CORR_DISPATCH),
], link(("Dispatch webhook", "Begin", 0), ("Begin", "Begin authorized?", 0), ("Begin authorized?", "Send email", 0),
        ("Send email", "Classify email result", 0), ("Classify email result", "Ack", 0)))


# --- W0 error handler ---------------------------------------------------------------------------

W0 = "00_error_handler"
w0 = workflow(ERROR_WORKFLOW_ID, "RF 00 Error handler", [
    {"id": nid(W0, "Error trigger"), "name": "Error trigger", "type": "n8n-nodes-base.errorTrigger", "typeVersion": 1,
     "position": [0, 300], "parameters": {}},
    http(W0, "Report to API", "POST", f"{API}/v1/ops/n8n-errors", [240, 300], strict=True, body=(
        "={{ JSON.stringify({ workflow_id: $json.workflow.id, workflow_name: $json.workflow.name, "
        "execution_id: String($json.execution.id ?? ''), mode: $json.execution.mode, "
        "last_node: $json.execution.lastNodeExecuted, "
        "message: String(($json.execution.error || {}).message || '').slice(0, 1000) }) }}")),
], link(("Error trigger", "Report to API", 0)))

# --- W5 recovery --------------------------------------------------------------------------------

W5 = "05_recovery"
w5 = workflow("RFw05Recovery001", "RF 05 Recovery sweep", [
    schedule(W5, "Every minute", {"field": "minutes", "minutesInterval": 1}, [0, 300]),
    http(W5, "Recovery sweep", "POST", f"{API}/v1/ops/recovery", [240, 300], strict=True, timeout=60000),
], link(("Every minute", "Recovery sweep", 0)))

# --- W6 daily digest ----------------------------------------------------------------------------

W6 = "06_digest"
PERIOD_JS = r"""
// One digest per UTC day. A manual run may name the period (validated by the API).
const j = $input.first().json;
const requested = j.body && j.body.period_key;
return [{ json: { period_key: requested || new Date().toISOString().slice(0, 10) } }];
"""
w6 = workflow("RFw06Digest00001", "RF 06 Daily digest", [
    schedule(W6, "Daily 07:00 UTC", {"field": "cronExpression", "expression": "0 7 * * *"}, [0, 200]),
    webhook(W6, "Run now webhook", "rf-digest-run", "dispatch", "onReceived", [0, 400]),
    code(W6, "Period key", PERIOD_JS, [240, 300]),
    http(W6, "Reserve digest", "POST", f"{API}/v1/ops/digests", [460, 300], strict=True,
         body="={{ JSON.stringify({ period_key: $json.period_key }) }}"),
    {"id": nid(W6, "Not sent yet?"), "name": "Not sent yet?", "type": "n8n-nodes-base.if", "typeVersion": 2.2,
     "position": [680, 300], "parameters": {"conditions": {
         "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 2},
         "conditions": [{"id": nid(W6, "send:c"), "leftValue": "={{ $json.send }}", "rightValue": "",
                         "operator": {"type": "boolean", "operation": "true", "singleValue": True}}],
         "combinator": "and"}, "options": {}}},
    {"id": nid(W6, "Send digest"), "name": "Send digest", "type": "n8n-nodes-base.emailSend", "typeVersion": 2.1,
     "position": [900, 200], "credentials": CRED["smtp"], "retryOnFail": False,
     "parameters": {"fromEmail": "RepairFlow <repairflow@repairflow.local>", "toEmail": "={{ $json.to }}",
                    "subject": "={{ $json.subject }}", "emailFormat": "text", "text": "={{ $json.text }}",
                    "options": {"appendAttribution": False}}},
    http(W6, "Mark sent", "POST", "=" + API + "/v1/ops/digests/{{ $('Reserve digest').first().json.period_key }}/sent",
         [1120, 200], strict=True, body="={{ JSON.stringify({ message_id: $json.messageId }) }}"),
], link(("Daily 07:00 UTC", "Period key", 0), ("Run now webhook", "Period key", 0), ("Period key", "Reserve digest", 0),
        ("Reserve digest", "Not sent yet?", 0), ("Not sent yet?", "Send digest", 0), ("Send digest", "Mark sent", 0)))


# --- W2 operator form ---------------------------------------------------------------------------

W2 = "02_operator_form"
FLEET = json.loads((ROOT / "data" / "equipment" / "equipment.json").read_text(encoding="utf-8"))["equipment"]
FORM_TO_REPORT_JS = r"""
// The form only collects the report; W1 owns intake (validation, idempotency, enrichment, triage).
// One submission = one event: a double click creates two events (documented limit of a plain form).
const f = $input.first().json;
const pick = (...names) => names.map((n) => f[n]).find((v) => v !== undefined && v !== '');
return [{ json: {
  event_id: `form-${$execution.id}`,
  equipment_id: String(pick('equipment_id', 'Equipment') || '').split(' ')[0],
  reported_at: new Date().toISOString().slice(0, 19) + 'Z',
  language: pick('language', 'Language') || 'fr',
  description: pick('description', 'What happened?'),
  operator_reference: pick('operator_reference', 'Your operator reference') || undefined,
} }];
"""
w2 = workflow("RFw02OperForm001", "RF 02 Operator report form", [
    {"id": nid(W2, "Operator form"), "name": "Operator form", "type": "n8n-nodes-base.formTrigger", "typeVersion": 2.2,
     "position": [0, 300], "webhookId": nid(W2, "form:rf-report"), "credentials": CRED["form"],
     "parameters": {
         # Form Trigger >= 2.2 reads its URL segment from options.path (read in FormTriggerV2.node.js).
         "authentication": "basicAuth", "formTitle": "Report an equipment problem",
         "formDescription": "Fictional demo fleet. Describe what you observed; a reviewer checks it before any ticket is created.",
         "formFields": {"values": [
             {"fieldLabel": "Equipment", "fieldName": "equipment_id", "fieldType": "dropdown", "requiredField": True,
              "fieldOptions": {"values": [{"option": f"{e['id']} ({e['display_name']}, {e['site']})"} for e in FLEET]}},
             {"fieldLabel": "Language", "fieldName": "language", "fieldType": "dropdown", "requiredField": True,
              "fieldOptions": {"values": [{"option": "fr"}, {"option": "en"}]}},
             {"fieldLabel": "What happened?", "fieldName": "description", "fieldType": "textarea", "requiredField": True},
             {"fieldLabel": "Your operator reference", "fieldName": "operator_reference", "fieldType": "text",
              "placeholder": "operator-demo-1"},
         ]},
         "responseMode": "onReceived",
         "options": {"respondWithOptions": {"values": {"respondWith": "text",
                     "formSubmittedText": "Report received. A reviewer will check it before any ticket is created."}},
                     "appendAttribution": False, "path": "rf-report"}}},
    code(W2, "Build report", FORM_TO_REPORT_JS, [240, 300]),
    http(W2, "Send to intake", "POST", "http://n8n:5678/webhook/rf-intake", [460, 300], cred="intake", strict=True,
         body="={{ JSON.stringify($json) }}", timeout=120000),
], link(("Operator form", "Build report", 0), ("Build report", "Send to intake", 0)))


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for fname, wf in (("00_error_handler.json", w0), ("01_intake.json", w1), ("02_operator_form.json", w2), ("03_dispatch_ticket.json", w3),
                      ("04_dispatch_notification.json", w4), ("05_recovery.json", w5), ("06_digest.json", w6)):
        (OUT / fname).write_text(json.dumps(wf, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
        print("wrote", OUT / fname)


if __name__ == "__main__":
    main()
