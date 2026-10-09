"""End-to-end demo against the running stack, through the real n8n webhook.

    python scripts/demo.py            # contradiction -> correction -> approval -> ticket + email
    python scripts/demo.py --outage   # same, with the ticket service failing twice (503) first

Prints what each independent system holds at the end (API state, ticket-service, Mailpit).
Requires only the standard library.
"""
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
ENV = dict(l.split("=", 1) for l in (ROOT / ".env").read_text().splitlines() if "=" in l and not l.startswith("#"))
API, N8N, TICKETS, MAILPIT = "http://127.0.0.1:18000", "http://127.0.0.1:15678", "http://127.0.0.1:18081", "http://127.0.0.1:18025"
REVIEWER = {"Authorization": f"Bearer {ENV['RF_REVIEWER_TOKEN']}"}


def call(method, url, body=None, headers=None):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def step(text):
    print(f"\n== {text}")


def main():
    outage = "--outage" in sys.argv
    event = {"event_id": f"evt-demo-{uuid.uuid4().hex[:8]}", "equipment_id": "robot-07",
             "reported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "language": "fr",
             "description": "Le robot s'arrête après trois minutes ; la batterie est pourtant pleine.",
             "operator_reference": "operator-demo-1"}

    step("1. operator report -> n8n intake webhook (twice: redelivery)")
    s1, r1 = call("POST", f"{N8N}/webhook/rf-intake", event, {"X-Intake-Key": ENV["RF_INTAKE_KEY"]})
    s2, r2 = call("POST", f"{N8N}/webhook/rf-intake", event, {"X-Intake-Key": ENV["RF_INTAKE_KEY"]})
    print(f"first: HTTP {s1}, replayed={r1.get('replayed')}; second: HTTP {s2}, replayed={r2.get('replayed')}; "
          f"same incident: {r1['incident_id'] == r2['incident_id']}")
    iid = r1["incident_id"]

    step("2. proposal (rules engine) with evidence")
    _, d = call("GET", f"{API}/v1/incidents/{iid}", headers=REVIEWER)
    p = d["review"]["proposal"]
    print(f"status={d['status']} revision={d['revision']} provenance={d['review']['provenance']} category={p['category']}")
    for c in p["contradictions"]:
        print(f"CONTRADICTION: {c['claim_a']} <-> {c['claim_b']}  refs={c['evidence_refs']}")
    for e in d["evidence"]:
        print(f"evidence {e['id']}: {e['kind']} observed {e['observed_at']}")

    step("3. reviewer corrects the ticket title (new revision), stale approval is refused")
    stale_digest = d["review"]["plan_digest"]
    call("POST", f"{API}/v1/incidents/{iid}/corrections", {"expected_revision": d["revision"],
         "note": "Charge LED is unreliable on this unit; trust telemetry.",
         "ticket_title": "[robot-07] battery: shutdown after 3 min, telemetry 12% vs reported full"}, REVIEWER)
    s, r = call("POST", f"{API}/v1/incidents/{iid}/approve", {"expected_revision": d["revision"], "plan_digest": stale_digest}, REVIEWER)
    print(f"approve with old revision -> HTTP {s} {r.get('error')}")
    _, d = call("GET", f"{API}/v1/incidents/{iid}", headers=REVIEWER)

    if outage:
        step("   injecting: ticket-service answers 503 twice")
        call("POST", f"{TICKETS}/_faults", {"mode": "fail_before", "status": 503, "count": 2}, {"X-Faults-Key": ENV["SIM_FAULTS_KEY"]})

    step(f"4. approve revision {d['revision']} (digest {d['review']['plan_digest'][:12]}...)")
    s, r = call("POST", f"{API}/v1/incidents/{iid}/approve",
                {"expected_revision": d["revision"], "plan_digest": d["review"]["plan_digest"]}, REVIEWER)
    print(f"HTTP {s}")

    step("5. waiting for worker -> n8n dispatch -> ticket-service / Mailpit -> ack")
    for _ in range(90):
        _, d = call("GET", f"{API}/v1/incidents/{iid}", headers=REVIEWER)
        if d["status"] in ("completed", "blocked"):
            break
        time.sleep(1)
    print(f"incident status: {d['status']}")
    for a in d["actions"]:
        print(f"  {a['type']:<12} {a['status']:<10} attempts={a['attempts']} ref={a['provider_reference']}")

    step("6. independent checks")
    _, tickets = call("GET", f"{TICKETS}/tickets?limit=500", headers={"X-Api-Key": ENV["TICKET_API_KEY"]})
    mine = [t for t in tickets if t["external_reference"] == iid]
    print(f"ticket-service: {len(mine)} ticket(s) for this incident: {[t['ticket_id'] for t in mine]}")
    notify = next(a for a in d["actions"] if a["type"] == "notify_team")
    marker = "RF-" + notify["id"].replace("-", "")[:12]
    _, found = call("GET", f"{MAILPIT}/api/v1/search?query=" + urllib.request.quote(f'subject:"{marker}"'))
    print(f"Mailpit: {len(found.get('messages') or [])} message(s) with marker {marker}")
    _, tl = call("GET", f"{API}/v1/incidents/{iid}/timeline", headers=REVIEWER)
    print("timeline:")
    for t in tl:
        print(f"  {t['at'][11:23]} {t['event']:<28} {t['actor'][:24]:<24} {(t['from'] or '')}->{(t['to'] or '')}")
    print(f"\nreview page: {API}/review   (token: RF_REVIEWER_TOKEN from .env)   Mailpit UI: {MAILPIT}")
    sys.exit(0 if d["status"] == "completed" and len(mine) == 1 else 1)


if __name__ == "__main__":
    main()
