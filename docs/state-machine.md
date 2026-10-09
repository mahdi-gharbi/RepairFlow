# State machines

Source of truth: `services/api/app/transitions.py` (tested in `tests/unit/test_core.py`). Any status change
goes through `check_incident` / `check_action`; an illegal transition raises and returns HTTP 409.

## Incident

```text
received ──evidence──> enriching ──triage──> needs_review ──approve──> approved ──begin──> dispatching ──all succeeded──> completed
    │                      │                  │  ▲   │                   │                    │
    │                      │                  │  └───┘ correction /      │ new revision       │ dead / uncertain email
    └──────(triage)────────┴──> needs_review  │       new evidence        ▼ (nothing started)  ▼
                                              └──reject──> rejected    needs_review         blocked ──resolve──> dispatching / completed
```

| From | Allowed targets |
|---|---|
| received | enriching, needs_review, rejected |
| enriching | needs_review, rejected |
| needs_review | needs_review (new revision), approved, rejected |
| approved | dispatching, needs_review (invalidated before start), blocked |
| dispatching | completed, blocked, needs_review |
| blocked | dispatching, needs_review, completed, rejected |
| completed, rejected | — (terminal) |

## Action

| From | Allowed targets | Meaning |
|---|---|---|
| pending | offered, cancelled | waiting in the outbox (or for a dependency: `available_at IS NULL`) |
| offered | executing, pending, retry_wait, cancelled | leased to a worker; `pending` again if the offer lease expires |
| executing | succeeded, retry_wait, uncertain, dead | `begin` succeeded: the provider may have been called. **Never cancelled.** |
| retry_wait | offered, cancelled, succeeded, dead | a failure *proven* without effect, waiting for backoff |
| uncertain | succeeded, retry_wait, dead | effect unknown: reconciliation or human decision |
| dead | retry_wait | explicit human requeue only |
| succeeded, cancelled | — | terminal |
