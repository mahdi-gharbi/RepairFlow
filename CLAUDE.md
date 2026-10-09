# CLAUDE.md — working rules for this repository

RepairFlow: n8n + FastAPI + PostgreSQL maintenance automation with versioned human approval, transactional
outbox and reconciliation. Plan of record: the milestone table in `PROGRESS.md`; keep it updated (steps,
commands, results, remaining work) after every milestone or significant finding. Working notes in French;
code, identifiers and README in English.

## Non-negotiable invariants

* PostgreSQL decides; n8n executes. No business decision in a workflow; no connector effect from the API.
* Every status change goes through `app/transitions.py`. Audit rows are written in the same transaction.
* Never hold a transaction across a network call. Lock order: incident → outbox → action.
* PostgreSQL `now()` is the only clock for leases and availability.
* Approval binds `(incident_revision, plan_digest)`; any new revision invalidates not-started actions.
* An `executing` action is never "cancelled"; unknown outcome = `uncertain`. Only connectors with an idempotent
  by-key lookup (tickets) are retried automatically after an unknown outcome. Never claim exactly-once.
* Recipients, priorities and allowed action types come from config/policy, never from report text or model output.
* The rules engine (`app/triage/rules.py`) must keep working without any LLM.
* Never present a mock, an unimported workflow JSON or an invented output as a real integration. The LLM is remote
  (Groq, key `GROQ_API_KEY` in `.env` only); tests use a labelled local provider double and never the real provider.

## Commands

```bash
docker compose up -d --build && bash scripts/provision_n8n.sh   # stack + n8n credentials/workflows
.venv/Scripts/python -m pytest -q tests/unit                    # no stack needed
.venv/Scripts/python -m pytest -q tests                         # live tests (stack running)
python scripts/demo.py [--outage]
```

* Workflows: edit `scripts/build_workflows.py`, then `bash scripts/provision_n8n.sh --from-src` (imports, publishes,
  exports and normalizes into `workflows/*.json`, regenerates `workflows/manifest.json`, scans for secrets).
  Node typeVersions must exist in the installed n8n (read them from `n8n-nodes-base/dist/nodes/*` in the image).
* Schema changes: edit `app/models`, then `alembic revision --autogenerate` with `RF_DATABASE_URL` pointing at
  `127.0.0.1:15432`; review the generated file. The API container runs `alembic upgrade head` at start.
* Git Bash on Windows: set `MSYS_NO_PATHCONV=1` for `docker compose exec/cp` with container paths.

## Layout

`services/api/app` (contracts, models, transitions, incidents, dispatch, policy, triage, routers, static/review.html),
`services/worker/main.py`, `services/sim/app.py` (ticket-service + telemetry-sim, fault injection),
`workflows/` (normalized n8n exports + manifest; `src/` = generator output), `data/equipment` (fictional fleet),
`tests/{unit,integration,faults}`, `docs/{architecture,reliability,state-machine}.md`.
