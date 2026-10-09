# Reliability: what is guaranteed, per connector, and what is not

There is **no global exactly-once guarantee**. The properties below were established by the tests named
in brackets, against the real PostgreSQL, API, n8n workflows and simulators described in `PROGRESS.md`.

## Intake

* `UNIQUE(source_scope, event_id)` + `INSERT .. ON CONFLICT DO NOTHING` inside the transaction that creates the
  incident. Same payload hash → `200` with the existing incident; different hash → `409`.
  [`test_20_concurrent_identical_intakes_create_one_incident`, `test_same_event_id_different_payload_is_409`]
* Two *different* events describing the same failure are **not** merged (no automatic dedup in V1).
* If W1 dies after the incident is stored, the worker triages it after 120 s with the rules engine and flags the
  missing telemetry. [`test_intake_interrupted_before_triage_is_recovered_by_worker`]

## Approval

* The reviewer approves `(expected_revision, plan_digest)`; the server recomputes the digest
  `SHA256(canonical_json{incident_id, revision, actions+frozen args, evidence versions, policy_version})`.
  Stale page → `409 stale_revision`; plan changed → `409 plan_changed`; second approval → `409`.
  [`test_concurrent_double_approval_creates_one_set_of_actions`]
* A new revision (evidence or correction) cancels not-started actions (`pending`, `offered`, `retry_wait`) and sends
  the incident back to review. `begin` re-checks the revision under lock, so an offer already in n8n is refused.
  [`test_new_evidence_before_begin_invalidates_approval`, `test_correction_after_approval_cancels_not_started_actions`]
* **Limit:** an action already `executing` may have reached the provider. It is not "cancelled"; the incident is
  `blocked` and the situation recorded (`approval_superseded_after_start`). An email cannot be recalled.

## Outbox, leases, fencing

* Claim: short transaction, `FOR UPDATE SKIP LOCKED`, lease + `fencing_token += 1` + random dispatch token, commit,
  then network call. `begin` is a compare-and-set `offered → executing` under row locks
  (lock order incident → outbox → action everywhere). [`test_concurrent_begin_with_the_same_token_authorizes_once`]
* An old worker/callback carrying a superseded fencing token is rejected and audited.
  [`test_stale_callback_after_new_lease_is_rejected`]
* A fencing token protects local state only; it cannot stop a provider call already in flight. That is why provider
  idempotency (tickets) or an explicit `uncertain` state (SMTP) is still needed.
* Leases: offer 30 s (time to call `begin`); executing lease per connector, from its real timeout:
  ticket 60 s (HTTP node timeout 10 s), email 180 s (n8n's SMTP node exposes no timeout; nodemailer waits up to 120 s
  to connect — observed: an SMTP outage made the node hang ~70 s instead of failing fast).
* Offer lease expired → `pending` (begin never happened, no effect). Executing lease expired → `uncertain`.
* A late ack of the *same* attempt (same fencing token) still resolves `uncertain`.
  [`test_late_ack_of_the_expired_attempt_resolves_uncertainty`]
* Retry policy is centralized in the API: `retryable` → exponential backoff with jitter (base 2 s, cap 300 s, max 5
  attempts → `dead`); `rate_limited` → `Retry-After` when present. n8n node retries are disabled.
  [`test_retryable_failures_back_off_then_die_after_max_attempts`, `test_rate_limit_honours_retry_after`,
  `test_rate_limit_retry_after_is_honoured`]

## Ticket connector (fictional ticket-service)

| Observation | Classified as | Effect |
|---|---|---|
| 200/201 | succeeded | provider reference stored |
| 503, 429 | retryable / rate_limited | contract of this simulator: rejected *before* processing |
| 409 | permanent | key reused with another payload → `dead`, human |
| other 4xx | permanent | `dead` |
| other 5xx, timeout | ambiguous → `uncertain` | worker looks up `GET /tickets/by-key/{action_key}`: found → succeeded; absent → re-dispatch (safe: same `Idempotency-Key`) |
| connection refused | retryable | nothing reached the provider |

`by-key` consistency in the simulator: strong (single PostgreSQL primary, read-after-write). A real provider may be
eventually consistent; an "absent" answer would then need a delay or a second check before re-dispatch.
[`test_lost_ack_after_remote_creation_is_reconciled_by_key`, `test_timeout_after_remote_creation_is_reconciled_without_duplicate`,
`test_5xx_after_processing_is_ambiguous_then_reconciled`, `test_ticket_service_503_twice_then_success`]

## Email connector (SMTP → Mailpit)

* SMTP offers no idempotency key. The n8n node cannot set a custom `Message-ID`, so each message carries a stable
  marker `[RF-<action id prefix>]` in the subject.
* Success = nodemailer `info.messageId` with a non-empty `accepted` list. Connection refused / DNS failure → retryable
  (no SMTP transaction started). Auth/recipient rejection → permanent. Anything else → `uncertain`, **no automatic retry**.
* Reconciliation of an uncertain email searches Mailpit for the marker. This is demo-only: a real relay offers no such
  lookup, so the decision would be a human's (`resolve: mark_succeeded | retry | give_up`, retry accepting duplicate risk).
* The email is a separate action, eligible only after the ticket succeeded. An email failure never re-creates the ticket.
  [`test_ambiguous_smtp_result_is_uncertain_and_never_auto_retried`, `test_email_failure_never_recreates_the_ticket`,
  `test_smtp_outage_delays_email_without_recreating_ticket`]

## Recovery, digest and n8n errors (W5, W6, W0)

* **W5** (Schedule Trigger, every minute) calls `POST /v1/ops/recovery`: lease expiry, reconciliation of uncertain
  tickets, recovery of stalled intakes. The worker runs the same code (`app/recovery.py`) every tick, so recovery does
  not depend on n8n's scheduler being up; running both concurrently is safe (row locks + state re-checks).
  [`test_recovery_schedule_runs_without_manual_trigger`]
* **W6** (daily 07:00 UTC, plus a manual webhook) reserves `digests.period_key` before sending, and marks it `sent` with
  the SMTP message id afterwards. A second run for a sent period sends nothing. **Limit:** if SMTP succeeds and the
  "mark sent" call is lost, the period stays `reserved` and a later run sends a second digest (accepted: a duplicate
  internal digest is harmless; a ticket is not). [`test_digest_is_sent_once_per_period_key`]
* **W0** (Error Trigger) is set as `errorWorkflow` on every workflow and posts failed executions to the audit log
  (`n8n_execution_failed`). Verified with a real failure (API 422 on an invalid period key). It cannot report when
  the API itself is down; it is a convenience, not a guaranteed callback. Business recovery never depends on it.
  [`test_error_workflow_reports_a_real_failed_execution`]

## Restarts

All state lives in PostgreSQL; API, worker and n8n can restart between approval and dispatch.
[`test_pending_actions_survive_api_worker_and_n8n_restarts`, `test_n8n_down_before_reception_then_recovered`]

## Audit

`audit_events` is append-only at application level (no update/delete code path) and written in the same transaction
as the state change it describes. It is not tamper-proof against a database administrator.
