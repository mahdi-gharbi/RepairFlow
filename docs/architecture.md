# Architecture and responsibility split

## Decision: who decides, who executes, who retries

| Component | Owns | Never does |
|---|---|---|
| **PostgreSQL** | All business state: incidents, immutable revisions, evidence snapshots, approvals, actions, outbox, audit. Also the **only clock** for leases (`now()` of the transaction). | — |
| **FastAPI** (`services/api`) | Validation (Pydantic, `extra="forbid"`), transitions (`app/transitions.py`), idempotent intake, versioned approval, `begin`/`ack` compare-and-set, reconciliation decisions, authorization. | Calls a connector to *create* an effect. Its only outbound calls are read-only lookups for reconciliation. |
| **n8n** (`workflows/`) | Orchestration and connectors: intake + enrichment (W1), ticket dispatch (W3), notification dispatch (W4). Holds connector credentials. | Decides whether an action is allowed. A 2xx from an n8n webhook means "received", not "done". |
| **Worker** (`services/worker`) | Claims outbox rows (`FOR UPDATE SKIP LOCKED`), leases them with a fencing token, posts them to n8n, expires leases, triggers reconciliation, recovers stalled intakes. | Keeps a transaction open across a network call. Retries an effect whose outcome is unknown without a by-key lookup. |
| **Simulators** (`services/sim`) | `ticket-service` (idempotent fictional ticketing API with fault injection) and `telemetry-sim`. | Exist outside local demo/test. Fault injection is not a business endpoint. |
| **Mailpit** | Local SMTP sink and message API. | Deliver anything outside the machine. |

Why this split: n8n is good at wiring connectors and showing executions, but its execution history is not
a durable business record (it can be pruned, an execution can die mid-way, a webhook can be called twice).
Every decision that must survive restarts and races is therefore taken inside a PostgreSQL transaction by
the API, and n8n is asked to perform exactly one approved effect per authorized `begin`.

## LLM placement

The structured-triage call is made by the API (`app/triage/llm.py`), not by an n8n node: recording the exact context,
validating references against the manifest, the single repair and the rules fallback all need the database, and the
call is read-only inference (no external effect). n8n W1 still orchestrates the enrichment order
(telemetry → manuals → triage). The API never holds a transaction while waiting for the model.

## Flow

```text
operator ──n8n form /form/rf-report (basic auth)──> W2 ──┐
operator/system ──POST /webhook/rf-intake (X-Intake-Key)──┴──> n8n W1   (single intake path)
  W1 ──POST /v1/incidents──────────────> API  (INSERT .. ON CONFLICT on (source_scope, event_id))
  W1 ──GET  telemetry-sim──> (available or not) ──POST /evidence/telemetry──> API (new revision)
  W1 ──POST /v1/incidents/{id}/evidence/manuals──> API (full-text retrieval, snapshot, new revision if changed)
  W1 ──POST /v1/incidents/{id}/triage──> API (LLM strict schema → validation → else rules engine)

reviewer ──/review page──> API: corrections (new revision) / approve(expected_revision, plan_digest)
  approve = one transaction: approval + actions(frozen args, action_key) + outbox rows

worker ──claim (SKIP LOCKED, lease, fencing+1, dispatch token)──> commit
worker ──POST /webhook/rf-dispatch-ticket (X-Dispatch-Key)──> n8n W3
  W3 ──POST /v1/actions/{id}/begin (fencing, token)──> API: CAS offered→executing, returns frozen args
  W3 ──POST ticket-service /tickets (Idempotency-Key = action_key)
  W3 ──classify status──> POST /v1/actions/{id}/ack ──> API: succeeded / retry_wait / uncertain / dead
  on success the notification's outbox row becomes eligible → W4 (Send Email → Mailpit) → ack
```

## Ports (all bound to 127.0.0.1)

| Service | Host port |
|---|---|
| API, dashboard, fallback review page | 18000 (`/app/`, `/review`, `/docs`) |
| n8n editor, webhooks, operator form (`/form/rf-report`) | 15678 |
| Mailpit UI/API | 18025 |
| ticket-service | 18081 |
| telemetry-sim | 18082 |
| PostgreSQL | 15432 |

## Correlation and retention

* Each incident has a `correlation_id`. W1 and W3/W4 send it as `X-Correlation-Id`; the API logs one JSON line per call
  (method, path, status, duration, correlation id — never the body), the worker logs it in its dispatch events and the
  ticket service in its requests. Grepping one id across `docker compose logs` gives the incident's path. Not distributed tracing.
* n8n prunes its execution history (14 days / 20 000 executions): business state never depends on it.
* `scripts/diagnostics.py` exports an allow-listed bundle (versions, counts, metrics, event ids/transitions); `scripts/reset_demo.py --yes`
  clears demo data and keeps configuration.

## Authentication

* Operator / reviewer: bearer tokens from `.env` (`RF_OPERATOR_TOKEN`, `RF_REVIEWER_TOKEN`), checked server-side with
  constant-time comparison; role checks in every route. The review page sends the token as a header (no cookie), so
  there is no ambient credential for CSRF; same-origin only, no CORS enabled.
* n8n → API: `X-RepairFlow-Internal` (separate secret, role `internal`).
* Client → n8n intake: Webhook node header auth `X-Intake-Key`. Worker → n8n dispatch: `X-Dispatch-Key`.
* Per-offer dispatch token: random, stored hashed, valid only with the current fencing token; never given to a model.
