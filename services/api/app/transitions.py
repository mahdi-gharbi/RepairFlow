"""Explicit transition tables. Every status change goes through `check_*`; anything else is a bug."""


class TransitionError(Exception):
    def __init__(self, kind: str, current: str, target: str):
        super().__init__(f"illegal {kind} transition {current} -> {target}")
        self.current, self.target = current, target


INCIDENT_TRANSITIONS: dict[str, set[str]] = {
    "received": {"enriching", "needs_review", "rejected"},
    # Enrichment failure keeps the incident and sends it to review.
    "enriching": {"needs_review", "rejected"},
    # needs_review -> needs_review: a correction / re-triage on a new revision.
    "needs_review": {"needs_review", "approved", "rejected"},
    # approved -> needs_review: newer evidence invalidated the approval before dispatch started.
    "approved": {"dispatching", "needs_review", "blocked"},
    "dispatching": {"completed", "blocked", "needs_review"},
    "blocked": {"dispatching", "needs_review", "completed", "rejected"},
    "completed": set(),
    "rejected": set(),
}

ACTION_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"offered", "cancelled"},
    # offered -> pending: lease expired before begin; no external effect can have started.
    "offered": {"executing", "pending", "retry_wait", "cancelled"},
    # executing: effect may have started at the provider; never "cancelled" from here.
    "executing": {"succeeded", "retry_wait", "uncertain", "dead"},
    "retry_wait": {"offered", "cancelled", "succeeded", "dead"},
    # uncertain is resolved by reconciliation or a human decision.
    "uncertain": {"succeeded", "retry_wait", "dead"},
    "succeeded": set(),
    "dead": {"retry_wait"},  # explicit human requeue only
    "cancelled": set(),
}

ACTION_NOT_STARTED = ("pending", "offered", "retry_wait")


def check_incident(current: str, target: str) -> None:
    if target not in INCIDENT_TRANSITIONS[current]:
        raise TransitionError("incident", current, target)


def check_action(current: str, target: str) -> None:
    if target not in ACTION_TRANSITIONS[current]:
        raise TransitionError("action", current, target)
