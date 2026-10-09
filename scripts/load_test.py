"""Small, documented load test on the local stack. Not a benchmark: one laptop, all services in Docker.

Phase 1 - intake: N reports posted to the API (internal path, rules triage, no LLM) with C concurrent clients.
Phase 2 - dispatch: M incidents approved at once; measures time until each is completed through
          outbox -> worker -> n8n W3/W4 -> ticket-service / Mailpit -> ack.

    python scripts/load_test.py --intake 200 --concurrency 20 --dispatch 30
Writes artifacts/demo/load_test_<UTC>.json.
"""
import argparse
import json
import pathlib
import platform
import statistics
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
ENV = dict(l.split("=", 1) for l in (ROOT / ".env").read_text().splitlines() if "=" in l and not l.startswith("#"))
API = "http://127.0.0.1:18000"
INTERNAL = {"X-RepairFlow-Internal": ENV["RF_INTERNAL_KEY"]}
REVIEWER = {"Authorization": f"Bearer {ENV['RF_REVIEWER_TOKEN']}"}
DESCRIPTION = "Le robot s'arrête après trois minutes ; la batterie est pourtant pleine."


def call(method, path, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, method=method, data=data, headers={"Content-Type": "application/json", **(headers or {})})
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"null"), time.perf_counter() - started, len(data or b"")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null"), time.perf_counter() - started, len(data or b"")


def pct(values, q):
    v = sorted(values)
    return round(v[min(len(v) - 1, int(round(q * (len(v) - 1))))] * 1000, 1)


def one_intake(n: int, tag: str):
    report = {"event_id": f"load-{tag}-{n}", "equipment_id": "robot-07", "language": "fr", "description": DESCRIPTION,
              "reported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    s, body, dt, size = call("POST", "/v1/incidents", {"source_scope": "load-test", "report": report}, INTERNAL)
    iid = body.get("incident_id") if s == 201 else None
    tel = {"source_id": "telemetry-sim", "available": True, "observed_at": datetime.now(timezone.utc).isoformat(),
           "values": {"battery_pct": 12, "network_online": True, "error_code": "E-PWR-02"}}
    t2 = t3 = 0.0
    if iid:
        _, _, t2, _ = call("POST", f"/v1/incidents/{iid}/evidence/telemetry", tel, INTERNAL)
        _, _, t3, _ = call("POST", f"/v1/incidents/{iid}/triage?engine=rules", None, INTERNAL)
    return s, iid, dt, dt + t2 + t3, size


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--intake", type=int, default=200)
    ap.add_argument("--concurrency", type=int, default=20)
    ap.add_argument("--dispatch", type=int, default=30)
    a = ap.parse_args()
    tag = uuid.uuid4().hex[:6]

    started = time.perf_counter()
    with ThreadPoolExecutor(a.concurrency) as pool:
        results = list(pool.map(lambda n: one_intake(n, tag), range(a.intake)))
    wall = time.perf_counter() - started
    ok = [r for r in results if r[0] == 201]
    intake = {"requests": a.intake, "concurrency": a.concurrency, "created": len(ok), "errors": a.intake - len(ok),
              "wall_s": round(wall, 2), "incidents_per_s": round(len(ok) / wall, 1),
              "create_ms_p50": pct([r[2] for r in ok], .5), "create_ms_p95": pct([r[2] for r in ok], .95),
              "create_evidence_triage_ms_p50": pct([r[3] for r in ok], .5),
              "create_evidence_triage_ms_p95": pct([r[3] for r in ok], .95),
              "payload_bytes": ok[0][4] if ok else None}
    print("intake:", json.dumps(intake))

    ids = [r[1] for r in ok[: a.dispatch]]
    approved_at = {}
    for iid in ids:
        _, d, _, _ = call("GET", f"/v1/incidents/{iid}", headers=REVIEWER)
        s, _, _, _ = call("POST", f"/v1/incidents/{iid}/approve",
                          {"expected_revision": d["revision"], "plan_digest": d["review"]["plan_digest"]}, REVIEWER)
        if s == 200:
            approved_at[iid] = time.perf_counter()
    done: dict[str, float] = {}
    deadline = time.perf_counter() + 600
    while len(done) < len(approved_at) and time.perf_counter() < deadline:
        for iid in approved_at:
            if iid in done:
                continue
            _, d, _, _ = call("GET", f"/v1/incidents/{iid}", headers=REVIEWER)
            if d["status"] in ("completed", "blocked"):
                done[iid] = time.perf_counter()
        time.sleep(1)
    lat = [done[i] - approved_at[i] for i in done]
    first = min(approved_at.values()) if approved_at else 0
    dispatch = {"approved": len(approved_at), "settled": len(done),
                "completed": sum(1 for i in done if call("GET", f"/v1/incidents/{i}", headers=REVIEWER)[1]["status"] == "completed"),
                "approval_to_settled_s_p50": round(statistics.median(lat), 2) if lat else None,
                "approval_to_settled_s_max": round(max(lat), 2) if lat else None,
                "incidents_per_min": round(len(done) / ((max(done.values()) - first) / 60), 1) if done else None,
                "note": "each incident = 2 actions (ticket then email), each a separate n8n execution; worker tick 1 s"}
    print("dispatch:", json.dumps(dispatch))

    # Leave the review queue clean: reject the load-test incidents that were not dispatched.
    for _, iid, *_ in ok[a.dispatch:]:
        _, d, _, _ = call("GET", f"/v1/incidents/{iid}", headers=REVIEWER)
        if d["status"] == "needs_review":
            call("POST", f"/v1/incidents/{iid}/reject", {"expected_revision": d["revision"], "reason": "load test"}, REVIEWER)
    out = ROOT / "artifacts" / "demo" / f"load_test_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    out.write_text(json.dumps({"intake": intake, "dispatch": dispatch, "client": platform.platform(),
                               "hardware": "i7-10510U (4C/8T), 20 GB RAM, Docker Desktop VM 10 GB, all services local"},
                              indent=2) + "\n", encoding="utf-8", newline="\n")
    print("wrote", out.relative_to(ROOT))


if __name__ == "__main__":
    main()
