"""Deterministic action policy. Priority, allowed action types and recipients come from here and
from configuration tables — never from report text or model output (a model 'confidence' is ignored)."""
from app.canonical import sha256_hex
from app.models import Equipment, Team

POLICY_VERSION = "policy-2026.10-1"

PRIORITY_BY_CATEGORY = {"battery": "P2", "mechanical": "P2", "sensor": "P3", "network": "P3", "other": "P4"}
ALLOWED_ACTIONS = ("create_ticket", "notify_team")


def build_plan(*, incident_id: str, revision: int, proposal: dict, overrides: dict,
               equipment: Equipment, team: Team) -> list[dict]:
    category = overrides.get("category") or proposal["category"]
    title = overrides.get("ticket_title") or proposal["draft_ticket"]["title"]
    description = overrides.get("ticket_description") or proposal["draft_ticket"]["description"]
    priority = PRIORITY_BY_CATEGORY[category]
    ticket = {
        "type": "create_ticket",
        "args": {
            "incident_id": incident_id,
            "incident_revision": revision,
            "equipment_id": equipment.id,
            "site": equipment.site,
            "category": category,
            "priority": priority,
            "title": title,
            "description": description,
        },
    }
    notify = {
        "type": "notify_team",
        "depends_on": "create_ticket",
        "args": {
            "incident_id": incident_id,
            "template": "ticket_created_v1",
            "to": team.notify_email,
            "team_id": team.id,
            "subject": f"[RepairFlow] {priority} {title}"[:200],
            "equipment_id": equipment.id,
        },
    }
    return [ticket, notify]


def plan_digest(*, incident_id: str, revision: int, plan: list[dict], evidence_versions: list[dict]) -> str:
    return sha256_hex({
        "incident_id": incident_id,
        "incident_revision": revision,
        "actions": [{"type": a["type"], "args": a["args"]} for a in plan],
        "evidence_source_versions": sorted(evidence_versions, key=lambda e: e["id"]),
        "policy_version": POLICY_VERSION,
    })
