"""Reset the local demo data (incidents, actions, audit, tickets, emails). Keeps configuration:
teams, equipment and the manual index. Refuses to run without --yes.

    python scripts/reset_demo.py --yes

n8n execution history is not touched (it is pruned by n8n itself, see compose.yaml).
"""
import os
import pathlib
import subprocess
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
BUSINESS_TABLES = ("outbox", "actions", "approvals", "triage_runs", "evidence_snapshots", "incident_revisions",
                   "inbox_events", "replay_runs", "incidents", "audit_events", "digests")


def compose(*args: str, stdin: str | None = None) -> None:
    env = {**os.environ, "MSYS_NO_PATHCONV": "1"}
    subprocess.run(["docker", "compose", *args], cwd=ROOT, check=True, input=stdin, text=True, env=env)


def main():
    if "--yes" not in sys.argv:
        sys.exit("This deletes every incident, action, audit event, ticket and email of the local demo. Re-run with --yes.")
    compose("stop", "worker")
    try:
        compose("exec", "-T", "postgres", "psql", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", "repairflow",
                stdin=f"TRUNCATE {', '.join(BUSINESS_TABLES)} RESTART IDENTITY CASCADE;\n")
        compose("exec", "-T", "postgres", "psql", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", "tickets",
                stdin="TRUNCATE tickets; ALTER SEQUENCE ticket_seq RESTART WITH 1000;\n")
        req = urllib.request.Request("http://127.0.0.1:18025/api/v1/messages", method="DELETE")
        urllib.request.urlopen(req, timeout=10).close()
    finally:
        compose("start", "worker")
    print("demo data reset: business tables truncated, tickets cleared, Mailpit emptied; configuration kept")


if __name__ == "__main__":
    main()
