"""Export a shareable diagnostic bundle: API diagnostics (allow-listed, no report text, no secrets),
image versions and n8n workflow activation. Fails if any .env value appears in the output.

    python scripts/diagnostics.py      -> artifacts/diagnostics_<UTC>.json
"""
import json
import os
import pathlib
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
ENV = dict(l.split("=", 1) for l in (ROOT / ".env").read_text().splitlines() if "=" in l and not l.startswith("#"))


def compose(*args: str) -> str:
    env = {**os.environ, "MSYS_NO_PATHCONV": "1"}
    return subprocess.run(["docker", "compose", *args], cwd=ROOT, check=True, capture_output=True, text=True, env=env).stdout


def main():
    req = urllib.request.Request("http://127.0.0.1:18000/v1/ops/diagnostics",
                                 headers={"Authorization": f"Bearer {ENV['RF_REVIEWER_TOKEN']}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        bundle = json.loads(r.read())
    raw = compose("images", "--format", "json").strip()
    images = json.loads(raw) if raw.startswith("[") else [json.loads(l) for l in raw.splitlines() if l.strip()]
    bundle["images"] = [{"service": i.get("ContainerName"), "image": f"{i.get('Repository')}:{i.get('Tag')}"} for i in images]
    workflows = compose("exec", "-T", "postgres", "psql", "-U", "postgres", "-d", "n8n", "-tAc",
                        "select id || '|' || name || '|' || active from workflow_entity order by id")
    bundle["n8n_workflows"] = [dict(zip(("id", "name", "active"), l.split("|"))) for l in workflows.splitlines() if l.strip()]

    text = json.dumps(bundle, indent=2)
    leaks = [k for k, v in ENV.items() if re.search(r"KEY|TOKEN|PASSWORD|SECRET", k) and v and v in text]
    if leaks:
        sys.exit(f"refusing to write: values of {leaks} found in the bundle")
    out = ROOT / "artifacts" / f"diagnostics_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    out.write_text(text + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {out.relative_to(ROOT)} ({len(bundle['recent_events'])} events, {len(bundle['recent_failures'])} failures); "
          "secret scan: clean")


if __name__ == "__main__":
    main()
