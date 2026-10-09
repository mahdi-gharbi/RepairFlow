#!/usr/bin/env bash
# Provision n8n from a clean state: credentials (from .env, never committed), workflows, publication.
# Then export what n8n actually stored to workflows/*.json and check that no secret leaked into it.
# Usage: bash scripts/provision_n8n.sh   (from the repository root, stack started with docker compose)
set -euo pipefail
export MSYS_NO_PATHCONV=1  # Git Bash on Windows: do not rewrite /tmp/... container paths
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a

WORKFLOWS=(
  "01_intake.json:RFw01Intake00001"
  "03_dispatch_ticket.json:RFw03DispTicket1"
  "04_dispatch_notification.json:RFw04DispNotify1"
)

wait_n8n() {
  for _ in $(seq 1 60); do
    if curl -fsS http://127.0.0.1:15678/healthz >/dev/null 2>&1; then return 0; fi
    sleep 2
  done
  echo "n8n did not become healthy" >&2; exit 1
}

PY=python; command -v "$PY" >/dev/null || PY=python3
"$PY" scripts/build_workflows.py

docker compose up -d n8n
wait_n8n

mkdir -p .provision
CREDS=.provision/credentials.json
trap 'rm -f "$CREDS"' EXIT
"$PY" - "$CREDS" <<'EOF'
import json, os, sys
creds = [
    {"id": "rfCredIntakeKey1", "name": "RF Intake Webhook Key", "type": "httpHeaderAuth",
     "data": {"name": "X-Intake-Key", "value": os.environ["RF_INTAKE_KEY"]}},
    {"id": "rfCredDispatchK1", "name": "RF Dispatch Webhook Key", "type": "httpHeaderAuth",
     "data": {"name": "X-Dispatch-Key", "value": os.environ["RF_DISPATCH_KEY"]}},
    {"id": "rfCredInternalK1", "name": "RF Internal API", "type": "httpHeaderAuth",
     "data": {"name": "X-RepairFlow-Internal", "value": os.environ["RF_INTERNAL_KEY"]}},
    {"id": "rfCredTicketKey1", "name": "RF Ticket API", "type": "httpHeaderAuth",
     "data": {"name": "X-Api-Key", "value": os.environ["TICKET_API_KEY"]}},
    {"id": "rfCredMailpitSm1", "name": "RF Mailpit SMTP", "type": "smtp",
     "data": {"user": "", "password": "", "host": "mailpit", "port": 1025, "secure": False, "disableStartTls": True}},
]
json.dump(creds, open(sys.argv[1], "w"))
EOF
docker compose cp "$CREDS" n8n:/tmp/rf-credentials.json
docker compose exec -T n8n n8n import:credentials --input=/tmp/rf-credentials.json
docker compose exec -T -u root n8n rm -f /tmp/rf-credentials.json
rm -f "$CREDS"

for entry in "${WORKFLOWS[@]}"; do
  file="${entry%%:*}"; id="${entry##*:}"
  docker compose exec -T n8n n8n import:workflow --input="/workflows/src/$file"
  docker compose exec -T n8n n8n publish:workflow --id="$id"
done

# Webhooks of published workflows are registered by the running server at startup.
docker compose restart n8n
wait_n8n

for entry in "${WORKFLOWS[@]}"; do
  file="${entry%%:*}"; id="${entry##*:}"
  docker compose exec -T n8n n8n export:workflow --id="$id" --pretty --output="/tmp/$file" >/dev/null
  docker compose cp "n8n:/tmp/$file" "workflows/$file"
done

# Secret scan: no value from .env may appear in an exported workflow.
leak=0
while IFS='=' read -r key value; do
  [[ -z "$key" || "$key" == \#* || -z "$value" ]] && continue
  if grep -rqF -- "$value" workflows/; then echo "SECRET LEAK: $key found in workflows/" >&2; leak=1; fi
done < .env
[[ $leak -eq 0 ]] || exit 1
echo "provisioned and exported: ${WORKFLOWS[*]}"
echo "secret scan: no .env value found in workflows/"
