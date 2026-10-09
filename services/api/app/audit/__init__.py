import json
import logging
import uuid

from sqlalchemy.orm import Session

from app.models import AuditEvent

log = logging.getLogger("repairflow")


def record(db: Session, event: str, actor: str, *, incident_id: uuid.UUID | None = None,
           action_id: uuid.UUID | None = None, correlation_id: uuid.UUID | None = None,
           from_state: str | None = None, to_state: str | None = None, payload: dict | None = None) -> None:
    """Adds an audit row to the caller's transaction, so it commits (or not) with the state change."""
    db.add(AuditEvent(incident_id=incident_id, action_id=action_id, correlation_id=correlation_id, event=event,
                      actor=actor, from_state=from_state, to_state=to_state, payload=payload or {}))
    log.info(json.dumps({"event": event, "actor": actor, "incident_id": str(incident_id) if incident_id else None,
                         "action_id": str(action_id) if action_id else None,
                         "correlation_id": str(correlation_id) if correlation_id else None,
                         "from": from_state, "to": to_state}))
