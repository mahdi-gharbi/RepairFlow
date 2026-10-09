"""Strip instance-specific metadata from n8n exports and write workflows/manifest.json.

Kept: what n8n needs to rebuild the workflow (id, name, nodes, connections, settings).
Dropped: sharing/project ids, timestamps, version ids, trigger counters (they belong to one instance).
"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
WF = ROOT / "workflows"
KEEP = ("id", "name", "description", "nodes", "connections", "settings", "pinData", "staticData", "meta", "tags")
N8N_VERSION = sys.argv[1] if len(sys.argv) > 1 else "unknown"
FILES = sorted(p.name for p in WF.glob("[0-9][0-9]_*.json"))

manifest = {
    "n8n_version": N8N_VERSION,
    "image": f"docker.n8n.io/n8nio/n8n:{N8N_VERSION}",
    "import": "bash scripts/provision_n8n.sh  (imports these files, publishes them, restarts n8n)",
    "notes": [
        "Production webhook URLs require the workflow to be published; test URLs (/webhook-test/) are not used.",
        "Credentials are created by the provisioning script from .env; only their names/ids appear here.",
    ],
    "workflows": [],
}
for name in FILES:
    path = WF / name
    data = json.loads(path.read_text(encoding="utf-8"))
    wf = data[0] if isinstance(data, list) else data
    clean = {k: wf[k] for k in KEEP if k in wf}
    clean["active"] = False  # activation is done by `n8n publish:workflow` during provisioning
    path.write_text(json.dumps(clean, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    creds = sorted({f"{t}:{c['name']}" for n in wf["nodes"] for t, c in (n.get("credentials") or {}).items()})
    hooks = [n["parameters"]["path"] for n in wf["nodes"] if n["type"] == "n8n-nodes-base.webhook"]
    manifest["workflows"].append({
        "file": name, "id": wf["id"], "name": wf["name"], "published_after_provisioning": True,
        "nodes": [{"name": n["name"], "type": n["type"], "typeVersion": n["typeVersion"]} for n in wf["nodes"]],
        "credentials_required": creds,
        "webhooks": [{"path": h, "production_url_internal": f"http://n8n:5678/webhook/{h}",
                      "production_url_host": f"http://127.0.0.1:15678/webhook/{h}"} for h in hooks],
    })
(WF / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
print(f"normalized {len(FILES)} exports, wrote workflows/manifest.json (n8n {N8N_VERSION})")
