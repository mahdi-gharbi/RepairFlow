"""Deterministic rule-based triage. Always available; the LLM path falls back here.

It reads the report text only to classify and to detect contradictions. Nothing in the text can
change which actions are allowed: that is decided by `app.policy` from configuration.
"""
import re
from datetime import datetime

from app.contracts import TriageProposal

ENGINE = "rules"
RULES_VERSION = "rules-2026.10-2"

KEYWORDS: dict[str, list[str]] = {
    "battery": [r"batter", r"charg", r"autonomie", r"power", r"alimentation", r"s'?arr[eê]te", r"shut ?down", r"[ée]teint"],
    "network": [r"wi-?fi", r"r[ée]seau", r"network", r"connexion", r"connection", r"signal", r"offline", r"d[ée]connect"],
    "mechanical": [r"roue", r"wheel", r"moteur", r"motor", r"bruit", r"noise", r"bloqu", r"stuck", r"vibr", r"\bbras\b", r"\barm\b", r"grinding"],
    "sensor": [r"capteur", r"sensor", r"lidar", r"cam[ée]ra", r"calibr", r"d[ée]tect", r"obstacle"],
}
# Order breaks ties deterministically.
CATEGORY_ORDER = ["battery", "mechanical", "sensor", "network"]

CLAIMS_BATTERY_FULL = re.compile(
    r"(batterie[^.;]{0,30}(pleine|charg[ée]e|100\s*%)|battery[^.;]{0,30}(full|charged|100\s*%)|full battery|fully charged)",
    re.IGNORECASE,
)
CLAIMS_NETWORK_OK = re.compile(r"(r[ée]seau[^.;]{0,20}(ok|fonctionne)|wi-?fi[^.;]{0,20}(ok|works|fine)|network[^.;]{0,20}(ok|fine|works))", re.IGNORECASE)

LOW_BATTERY_PCT = 25


def classify(text: str, telemetry: dict | None) -> str:
    scores = {c: sum(1 for k in KEYWORDS[c] if re.search(k, text, re.IGNORECASE)) for c in CATEGORY_ORDER}
    values = (telemetry or {}).get("values") or {}
    if isinstance(values.get("battery_pct"), (int, float)) and values["battery_pct"] < LOW_BATTERY_PCT:
        scores["battery"] += 1
    best = max(CATEGORY_ORDER, key=lambda c: (scores[c], -CATEGORY_ORDER.index(c)))
    return best if scores[best] > 0 else "other"


def run(*, incident_id: str, equipment_id: str, revision: int, data: dict, message_ref: str,
        telemetry: dict | None, telemetry_ref: str | None, manifest_id: str, reported_at: datetime,
        max_age_s: int, manuals: list[tuple[str, str, dict]] = ()) -> TriageProposal:
    """`manuals`: (evidence_ref, version, snapshot content) of attached manual sections."""
    text: str = data["description"]
    facts, missing, contradictions = [], [], []

    facts.append({"claim": f"Operator report: {text[:300]}", "evidence_refs": [message_ref]})

    values = {}
    if telemetry is None or telemetry_ref is None:
        missing.append("No telemetry snapshot was recorded for this equipment.")
    elif not telemetry.get("available"):
        missing.append(f"Telemetry source unavailable at intake ({telemetry.get('error') or 'unknown error'}).")
    else:
        values = telemetry.get("values") or {}
        observed = telemetry.get("observed_at")
        if observed:
            observed_dt = datetime.fromisoformat(observed)
            age = (reported_at - observed_dt).total_seconds()
            if age > max_age_s:
                missing.append(f"Telemetry is stale: observed {int(age // 60)} min before the report.")
        for key in ("battery_pct", "network_online", "error_code", "uptime_s"):
            if key in values:
                facts.append({"claim": f"Telemetry {key}={values[key]} (observed {observed})", "evidence_refs": [telemetry_ref]})

    battery = values.get("battery_pct")
    if CLAIMS_BATTERY_FULL.search(text) and isinstance(battery, (int, float)) and battery < LOW_BATTERY_PCT:
        contradictions.append({
            "claim_a": "Operator states the battery is full.",
            "claim_b": f"Telemetry reports battery_pct={battery}.",
            "evidence_refs": [message_ref, telemetry_ref],
        })
    if CLAIMS_NETWORK_OK.search(text) and values.get("network_online") is False:
        contradictions.append({
            "claim_a": "Operator states the network works.",
            "claim_b": "Telemetry reports network_online=false.",
            "evidence_refs": [message_ref, telemetry_ref],
        })
    if len(text.strip()) < 20:
        missing.append("Symptom description is too short to act on.")

    for ref, version, content in manuals:
        first = re.split(r"(?<=[.!?])\s", content["text"], maxsplit=1)[0]
        facts.append({"claim": f"Manual '{content['title']}' v{version}, section '{content['section']}': {first[:250]}",
                      "evidence_refs": [ref]})
    if not manuals:
        missing.append("No matching maintenance documentation is attached for this equipment model.")

    category = data.get("category_override") or classify(text, telemetry)
    next_step = "clarify" if (contradictions or missing) else "prepare_ticket"
    summary = f"{category} issue reported on {equipment_id}"
    if contradictions:
        summary += f"; {len(contradictions)} contradiction(s) between report and telemetry"
    if missing:
        summary += f"; {len(missing)} missing information item(s)"

    lines = [summary + ".", "", "Facts:"] + [f"- {f['claim']} [{', '.join(f['evidence_refs'])}]" for f in facts]
    if contradictions:
        lines += ["", "Contradictions:"] + [f"- {c['claim_a']} / {c['claim_b']} [{', '.join(c['evidence_refs'])}]" for c in contradictions]
    if missing:
        lines += ["", "Missing information:"] + [f"- {m}" for m in missing]

    first_line = re.sub(r"\s+", " ", text).strip()[:70]
    return TriageProposal(
        incident_revision=revision,
        category=category,
        summary=summary,
        extracted_facts=facts,
        missing_information=missing,
        contradictions=contradictions,
        suggested_next_step=next_step,
        draft_ticket={"title": f"[{equipment_id}] {category}: {first_line}", "description": "\n".join(lines)},
        evidence_manifest_id=manifest_id,
    )
