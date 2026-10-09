#!/usr/bin/env bash
# Provision n8n: credentials (from .env, never committed), workflows, publication, restart.
#   bash scripts/provision_n8n.sh             import the committed exports workflows/*.json (default)
#   bash scripts/provision_n8n.sh --from-src  regenerate from scripts/build_workflows.py, import, then
#                                             export what n8n stored back to workflows/*.json
# Always ends with a secret scan of workflows/.
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

FROM_SRC=0; [[ "${1:-}" == "--from-src" ]] && FROM_SRC=1
PY=python; command -v "$PY" >/dev/null || PY=python3
if [[ $FROM_SRC -eq 1 ]]; then "$PY" scripts/build_workflows.py; SRC_DIR=/workflows/src; else SRC_DIR=/workflows; fi

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
  docker compose exec -T n8n n8n import:workflow --input="$SRC_DIR/$file"
  docker compose exec -T n8n n8n publish:workflow --id="$id"
done

# Webhooks of published workflows are registered by the running server at startup.
docker compose restart n8n
wait_n8n
# /healthz answers before webhooks are registered: wait for 403 (registered, auth required) instead of 404.
for _ in $(seq 1 60); do
  code=$(curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:15678/webhook/rf-intake || true)
  [[ "$code" == "403" ]] && break
  sleep 1
done
[[ "$code" == "403" ]] || { echo "intake webhook not registered (HTTP $code)" >&2; exit 1; }

if [[ $FROM_SRC -eq 1 ]]; then
  for entry in "${WORKFLOWS[@]}"; do
    file="${entry%%:*}"; id="${entry##*:}"
    docker compose exec -T n8n n8n export:workflow --id="$id" --pretty --output="/tmp/$file" >/dev/null
    docker compose cp "n8n:/tmp/$file" "workflows/$file"
  done
  "$PY" scripts/normalize_exports.py "$(docker compose exec -T n8n n8n --version | tr -d '')"
fi

# Published = active in the n8n database (what the running server registered at startup).
docker compose exec -T postgres psql -U postgres -d n8n -tAc   "select id || ' active=' || active from workflow_entity where id like 'RFw%' order by id"

# Secret scan: no value from .env may appear in an exported workflow.
leak=0
while IFS='=' read -r key value; do
  [[ -z "$key" || "$key" == \#* || -z "$value" ]] && continue
  if grep -rqF -- "$value" workflows/; then echo "SECRET LEAK: $key found in workflows/" >&2; leak=1; fi
done < .env
[[ $leak -eq 0 ]] || exit 1
echo "provisioned: ${WORKFLOWS[*]} (from_src=$FROM_SRC)"
echo "secret scan: no .env value found in workflows/"
