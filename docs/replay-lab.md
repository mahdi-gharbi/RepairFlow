# Incident Replay Lab

"What would have happened with this fault, this evidence change or this other rule?" — answered on a
copy of an incident, without touching the real one.

## How it works

`POST /v1/replays {incident_id, scenario, mode, seed}` (reviewer) runs `app/replay.py` synchronously and stores a
`replay_runs` row (source summary, replay summary, comparison, step trace). The dashboard's **Replay** tab drives it.

* **Source snapshot**: the revision covered by the latest approval (or the current one), its evidence snapshots, the
  recorded triage runs and the approval digest. Snapshots are immutable, so a replay can be repeated later.
* **Same decision code as live**: rules engine, reference validation, policy (`build_plan`), plan digest, transition
  tables and `dispatch.decide` — the function `/ack` itself uses to turn an attempt outcome into a state.
* **Sandbox connectors**: an in-memory ticket provider with the documented idempotency semantics and an in-memory
  mailbox. Action keys are `replay:<run id>:<type>`.
* **Isolation is structural**: the simulator only reads the source incident; it never inserts `actions` or `outbox`
  rows and never calls n8n, the ticket service or SMTP. No button can re-trigger an effect of the original run.
  [`tests/integration/test_replay.py::test_replays_never_create_real_actions`]

## Modes

| Mode | Proposal | Deterministic |
|---|---|---|
| `frozen` | re-validates the LLM output recorded for the source run against the sandbox evidence (rejected → rules) | yes, for a given seed (backoff jitter is seeded) |
| `rules` | rules engine only | yes |
| `requery` | asks the configured LLM again with the sandbox context | **no** — hosted models vary even at temperature 0 |

## Scenarios

| Scenario | What changes | What the comparison shows (observed on demo incidents) |
|---|---|---|
| `baseline` | nothing | no difference, approval digest still valid |
| `ticket_503_twice` | ticket provider returns 503 twice | 3 attempts with backoff, 1 ticket |
| `duplicate_delivery_x5` | same event delivered 5× + 1 altered copy | 1 created, 4 replayed, 1 conflict |
| `new_manual_after_approval` | a cited manual gets a new version before dispatch | not-started actions cancelled, new review required |
| `telemetry_unavailable` / `telemetry_stale` | telemetry evidence replaced | different proposal (missing info), digest changed → new review |
| `crash_after_remote_creation` | ticket created, ack lost | lease expiry → `uncertain` → reconciled by key, 1 ticket |
| `llm_malformed_output` / `llm_invented_citation` | recorded model output corrupted | output rejected, rules fallback, digest changed → new review |
| `malicious_instruction` | instruction appended to the report | authorized actions, priority and recipient unchanged |
| `email_accepted_response_lost` | SMTP accepts, response lost | email `uncertain`, no automatic retry, human decision needed |
| `policy_clarify_on_contradiction` | alternative policy | no action authorized while a contradiction is open |

## Limits

* The simulator models connector behaviour from the documented contracts; it does not execute the n8n JavaScript
  classifiers. The live fault-injection tests (`tests/faults`) cover the real n8n workflows.
* Incident-level transitions are simplified to the dispatch path; action transitions are checked with the live table.
* `requery` spends provider quota and is subject to its rate limit.
