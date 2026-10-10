# RepairFlow — evidence-based maintenance automation

RepairFlow turns an operator's message about a robot ("it stops after three minutes, the battery is full") into a
maintenance case: equipment context, dated evidence, contradictions, missing information, a ticket proposal and a
**versioned human approval** — then executes the approved actions through **n8n** with durable retries and
reconciliation when a provider fails or answers ambiguously.

> The question it answers: *how do you automate repetitive work without losing control when a model is wrong or an
> API goes down?*

**Status (2026-10-09):** milestones M0–M5 done: real n8n workflows (intake, dispatch, recovery, digest, error handler),
outbox and reconciliation, fault-injection tests, manual retrieval (PostgreSQL full-text), structured LLM proposals with
a rules fallback, a React dashboard with live updates, an Incident Replay Lab and a first evaluation campaign (draft
labels, see below). The LLM is a **remote** model (Groq, `openai/gpt-oss-20b`) by the owner's choice; switching to a
local OpenAI-compatible server (Ollama) is a configuration change. Details: [`PROGRESS.md`](PROGRESS.md). All connectors are local: a fictional ticket service and Mailpit. Fleet, sites and
teams are fictional.

## What the demo shows

1. A report arrives twice through the n8n intake webhook → one incident (idempotent on `event_id`).
2. Telemetry says `battery_pct=12`: the proposal flags the **contradiction** instead of inventing a cause, citing both
   evidence snapshots.
3. The reviewer corrects the ticket title (new revision); approving the old page is refused (`409 stale_revision`).
4. The ticket service fails twice (503): the worker retries with backoff; one ticket is created; then the team is
   notified by email.
5. A timeout *after* the ticket was created: the action becomes `uncertain`, is reconciled by idempotency key, and no
   duplicate is created. An ambiguous SMTP result is **not** retried blindly.

```bash
python scripts/init_env.py
docker compose up -d --build
bash scripts/provision_n8n.sh
python scripts/demo.py --outage
```

Dashboard: <http://127.0.0.1:18000/app/> (token `RF_REVIEWER_TOKEN` or `RF_OPERATOR_TOKEN` from `.env`) · Operator form (n8n):
<http://127.0.0.1:15678/form/rf-report> (`RF_FORM_USER` / `RF_FORM_PASSWORD`) · Mailpit: <http://127.0.0.1:18025>

6. **Replay Lab**: re-run the incident in a sandbox under a fault or another rule ("what if the SMTP response was lost?")
   and compare decisions side by side — without touching the real incident ([`docs/replay-lab.md`](docs/replay-lab.md)).

## Architecture

```text
report ─> n8n W1 intake ─> FastAPI/PostgreSQL <── reviewer (approve revision + plan digest)
                                │ one transaction: approval + actions + outbox
                                v
                          worker (SKIP LOCKED, lease, fencing token)
                                │ POST dispatch webhook ("received", not "done")
                                v
               n8n W3 ticket / W4 email: begin (CAS) → connector → ack
```

* **PostgreSQL** holds every decision and is the only clock. **n8n** runs connectors. **The worker** re-offers,
  expires leases and reconciles. Details: [`docs/architecture.md`](docs/architecture.md).
* Guarantees differ per connector (idempotent ticket API vs SMTP): [`docs/reliability.md`](docs/reliability.md).
* LLM configuration, observed runs and limits: [`docs/model-card.md`](docs/model-card.md).
* Evaluation protocol, results and their limits (labels not yet human-reviewed): [`evaluation/README.md`](evaluation/README.md).
* State machines: [`docs/state-machine.md`](docs/state-machine.md). n8n exports: [`workflows/`](workflows/) with
  [`manifest.json`](workflows/manifest.json) (n8n 2.42.6).

## Tests

```bash
python -m venv .venv && .venv/Scripts/pip install -r services/api/requirements.txt pytest
.venv/Scripts/python -m pytest -q tests        # 73 tests: unit, PostgreSQL integration, LLM contract, replay, n8n faults/ops
cd apps/dashboard && npm ci && npx playwright install chromium && npx playwright test   # 3 end-to-end journeys
```

Integration tests cover 20 concurrent identical intakes, concurrent double approval, stale approval after new
evidence, stale callbacks after a new lease, lost acknowledgements, SMTP ambiguity and restarts of API/worker/n8n.
They establish properties of the tested scenarios and providers, not a universal absence of duplicates.

## Stack

n8n Community 2.42.6 (self-hosted) · Groq API (OpenAI-compatible, strict JSON schema) · React 18 + TypeScript (Vite) · FastAPI · Pydantic · SQLAlchemy · Alembic · PostgreSQL 17 · Mailpit ·
Docker Compose · pytest · Playwright.
