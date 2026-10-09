"""Local test doubles for external providers. SIM_ROLE selects which one this process serves.

tickets   — fictional ticketing API. Contract (docs/reliability.md):
            POST /tickets requires Idempotency-Key. Same key + same payload -> same ticket (200 replay).
            Same key + different payload -> 409. 503 and 429 mean "rejected before processing" (no effect).
            Any other failure (timeout, 5xx) leaves the effect unknown. GET /tickets/by-key/{key} is
            strongly consistent (read-after-write on a single PostgreSQL primary).
telemetry — simulated equipment telemetry from data/equipment/telemetry.json.

Fault injection (/_faults) exists only when SIM_FAULTS_ENABLED=true and is never part of the business API.
"""
import asyncio
import hashlib
import json
import os
import pathlib
import secrets
import threading
from datetime import datetime, timedelta, timezone

import psycopg
from fastapi import FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

ROLE = os.environ["SIM_ROLE"]
FAULTS_ENABLED = os.environ.get("SIM_FAULTS_ENABLED", "false") == "true"
FAULTS_KEY = os.environ.get("SIM_FAULTS_KEY", "")
DATA = pathlib.Path(os.environ.get("RF_DATA_DIR", "/data"))

app = FastAPI(title=f"RepairFlow simulator ({ROLE})")
_faults: list[dict] = []
_lock = threading.Lock()


def _check(given: str | None, expected: str) -> None:
    if not given or not expected or not secrets.compare_digest(given.encode(), expected.encode()):
        raise HTTPException(401, "unauthorized")


class Fault(BaseModel):
    # fail_before: respond `status` without any effect. fail_after: create, then respond `status`.
    # delay_after: create, then wait `delay_s` before answering (client timeout). unavailable: telemetry 503.
    mode: str = Field(pattern="^(fail_before|fail_after|delay_after|unavailable)$")
    count: int = Field(default=1, ge=1, le=100)
    status: int = Field(default=503, ge=400, le=599)
    delay_s: float = Field(default=15, ge=0, le=120)
    retry_after_s: int | None = None


def _take_fault() -> dict | None:
    with _lock:
        if not _faults:
            return None
        fault = _faults[0]
        fault["count"] -= 1
        if fault["count"] <= 0:
            _faults.pop(0)
        return dict(fault)


if FAULTS_ENABLED:
    @app.post("/_faults")
    def add_fault(fault: Fault, x_faults_key: str | None = Header(default=None)):
        _check(x_faults_key, FAULTS_KEY)
        with _lock:
            _faults.append(fault.model_dump())
        return {"queued": len(_faults)}

    @app.delete("/_faults")
    def clear_faults(x_faults_key: str | None = Header(default=None)):
        _check(x_faults_key, FAULTS_KEY)
        with _lock:
            _faults.clear()
        return {"queued": 0}


@app.get("/healthz")
def healthz():
    return {"ok": True, "role": ROLE}


# --- tickets ------------------------------------------------------------------------------------

if ROLE == "tickets":
    DSN = os.environ["TICKETS_DATABASE_URL"]
    API_KEY = os.environ["TICKET_API_KEY"]

    @app.on_event("startup")
    def init_db():
        with psycopg.connect(DSN) as c:
            c.execute("""
                CREATE SEQUENCE IF NOT EXISTS ticket_seq START 1000;
                CREATE TABLE IF NOT EXISTS tickets (
                    ticket_id text PRIMARY KEY,
                    idempotency_key text NOT NULL UNIQUE,
                    payload_hash text NOT NULL,
                    payload jsonb NOT NULL,
                    created_at timestamptz NOT NULL DEFAULT now()
                )""")

    class TicketIn(BaseModel):
        title: str = Field(min_length=3, max_length=160)
        description: str = Field(max_length=8000)
        priority: str = Field(pattern="^P[1-4]$")
        equipment_id: str = Field(max_length=64)
        site: str = Field(max_length=200)
        category: str = Field(max_length=32)
        external_reference: str = Field(max_length=200)

    def _row(r) -> dict:
        return {"ticket_id": r[0], "idempotency_key": r[1], "created_at": r[2].isoformat(), **r[3]}

    def _create(key: str, payload: dict) -> tuple[int, dict]:
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        with psycopg.connect(DSN) as c:
            row = c.execute(
                """INSERT INTO tickets (ticket_id, idempotency_key, payload_hash, payload)
                   VALUES ('TCK-' || nextval('ticket_seq'), %s, %s, %s)
                   ON CONFLICT (idempotency_key) DO NOTHING
                   RETURNING ticket_id, idempotency_key, created_at, payload""",
                (key, digest, json.dumps(payload))).fetchone()
            if row:
                return 201, _row(row)
            existing = c.execute("SELECT ticket_id, idempotency_key, created_at, payload, payload_hash FROM tickets "
                                 "WHERE idempotency_key = %s", (key,)).fetchone()
            if existing[4] != digest:
                return 409, {"error": "idempotency_key_reused_with_different_payload", "ticket_id": existing[0]}
            return 200, _row(existing)

    @app.post("/tickets")
    async def create_ticket(ticket: TicketIn, response: Response, x_api_key: str | None = Header(default=None),
                            idempotency_key: str | None = Header(default=None)):
        _check(x_api_key, API_KEY)
        if not idempotency_key or len(idempotency_key) > 200:
            raise HTTPException(400, "Idempotency-Key header required")
        fault = _take_fault()
        if fault and fault["mode"] == "fail_before":
            headers = {"Retry-After": str(fault["retry_after_s"])} if fault.get("retry_after_s") else {}
            raise HTTPException(fault["status"], "injected fault before processing", headers=headers)
        status, body = await asyncio.to_thread(_create, idempotency_key, ticket.model_dump())
        if fault and fault["mode"] == "fail_after":
            raise HTTPException(fault["status"], "injected fault after processing")
        if fault and fault["mode"] == "delay_after":
            await asyncio.sleep(fault["delay_s"])
        response.status_code = status
        if status == 200:
            response.headers["Idempotent-Replayed"] = "true"
        return body

    @app.get("/tickets/by-key/{key}")
    def by_key(key: str, x_api_key: str | None = Header(default=None)):
        _check(x_api_key, API_KEY)
        with psycopg.connect(DSN) as c:
            r = c.execute("SELECT ticket_id, idempotency_key, created_at, payload FROM tickets WHERE idempotency_key = %s",
                          (key,)).fetchone()
        if r is None:
            raise HTTPException(404, "no ticket for this key")
        return _row(r)

    @app.get("/tickets")
    def list_tickets(x_api_key: str | None = Header(default=None), limit: int = 50):
        _check(x_api_key, API_KEY)
        with psycopg.connect(DSN) as c:
            rows = c.execute("SELECT ticket_id, idempotency_key, created_at, payload FROM tickets "
                             "ORDER BY created_at DESC LIMIT %s", (min(limit, 500),)).fetchall()
        return [_row(r) for r in rows]


# --- telemetry ----------------------------------------------------------------------------------

if ROLE == "telemetry":
    FIXTURES = json.loads((DATA / "equipment" / "telemetry.json").read_text(encoding="utf-8"))

    @app.get("/telemetry/{equipment_id}")
    def telemetry(equipment_id: str):
        fault = _take_fault()
        if fault and fault["mode"] in ("unavailable", "fail_before"):
            raise HTTPException(fault["status"], "telemetry source unavailable (injected)")
        entry = FIXTURES.get(equipment_id)
        if entry is None or equipment_id.startswith("_"):
            raise HTTPException(404, "unknown equipment")
        observed = datetime.now(timezone.utc) + timedelta(seconds=entry["observed_offset_s"])
        return {"source_id": "telemetry-sim", "equipment_id": equipment_id,
                "observed_at": observed.isoformat(), "values": entry["values"]}
