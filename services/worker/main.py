"""Durable dispatcher. PostgreSQL holds every command; this loop can crash and restart at any point.

Each tick: (1) expire leases, (2) reconcile uncertain actions that have a by-key lookup,
(3) claim eligible outbox rows and hand them to the matching n8n dispatch webhook.
A 2xx from n8n only means "received": the result arrives later through /begin and /ack.
"""
import json
import logging
import os
import signal
import socket
import time
import uuid

import httpx

from app import dispatch, recovery
from app.db import session

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("worker")

OWNER = f"worker:{socket.gethostname()}:{os.getpid()}"
N8N = os.environ.get("RF_N8N_URL", "http://n8n:5678")
WEBHOOKS = {
    "create_ticket": f"{N8N}/webhook/rf-dispatch-ticket",
    "notify_team": f"{N8N}/webhook/rf-dispatch-notification",
}
DISPATCH_KEY = os.environ["RF_DISPATCH_KEY"]
TICK_S = float(os.environ.get("RF_WORKER_TICK_S", "1.0"))
RECONCILE_EVERY_S = float(os.environ.get("RF_RECONCILE_EVERY_S", "10"))
STALLED_INTAKE_S = int(os.environ.get("RF_STALLED_INTAKE_S", "120"))

running = True


def stop(*_):
    global running
    running = False


def emit(event: str, **fields):
    log.info(json.dumps({"component": "worker", "event": event, **fields}))


def deliver(client: httpx.Client, offer: dict) -> None:
    url = WEBHOOKS[offer["type"]]
    body = {k: offer[k] for k in ("action_id", "fencing_token", "dispatch_token", "correlation_id")}
    started = time.monotonic()
    try:
        r = client.post(url, json=body, headers={"X-Dispatch-Key": DISPATCH_KEY}, timeout=5)
    except httpx.ConnectError as exc:
        # Nothing reached n8n: safe to requeue immediately with backoff.
        with session() as db:
            dispatch.release_offer(db, offer_id(offer), offer["fencing_token"], OWNER, f"connect_error:{type(exc).__name__}")
        emit("dispatch_not_received", action_id=offer["action_id"], reason=type(exc).__name__)
        return
    except httpx.HTTPError as exc:
        # Timeout etc.: n8n may have received it. Leave it offered; the lease decides.
        emit("dispatch_ambiguous", action_id=offer["action_id"], reason=type(exc).__name__)
        return
    ms = int((time.monotonic() - started) * 1000)
    if r.status_code == 404:  # webhook not registered/active: provably not executed
        with session() as db:
            dispatch.release_offer(db, offer_id(offer), offer["fencing_token"], OWNER, "webhook_not_registered")
    emit("dispatch_posted", action_id=offer["action_id"], type=offer["type"], http_status=r.status_code,
         fencing_token=offer["fencing_token"], correlation_id=offer["correlation_id"], duration_ms=ms)


def offer_id(offer: dict) -> uuid.UUID:
    return uuid.UUID(offer["action_id"])


def reconcile_uncertain(ids=None) -> None:
    for result in recovery.reconcile_uncertain(OWNER, ids):
        emit("reconciled", **result)


def recover_stalled_intake() -> None:
    for result in recovery.recover_stalled_intake(OWNER, STALLED_INTAKE_S):
        emit("stalled_intake_recovered" if result["recovered"] else "stalled_intake_skipped", **result)


def main() -> None:
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    emit("started", owner=OWNER)
    last_reconcile = 0.0
    with httpx.Client() as client:
        while running:
            try:
                with session() as db:
                    expired = dispatch.sweep_expired(db, OWNER)
                if expired:
                    reconcile_uncertain(expired)
                if time.monotonic() - last_reconcile > RECONCILE_EVERY_S:
                    reconcile_uncertain()
                    recover_stalled_intake()
                    last_reconcile = time.monotonic()
                with session() as db:
                    offers = dispatch.claim(db, OWNER)
                for offer in offers:
                    deliver(client, offer)
            except Exception as exc:  # keep the loop alive; state is in PostgreSQL
                emit("tick_error", error=f"{type(exc).__name__}: {exc}")
                time.sleep(2)
            time.sleep(TICK_S)
    emit("stopped")


if __name__ == "__main__":
    main()
