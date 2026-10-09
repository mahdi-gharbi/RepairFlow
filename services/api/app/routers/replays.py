import uuid

from fastapi import APIRouter, Depends
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit, replay
from app.auth import Principal, principal, require
from app.contracts import Strict
from app.db import get_db
from app.errors import NotFound
from app.models import ReplayRun

router = APIRouter(prefix="/v1")


class ReplayRequest(Strict):
    incident_id: uuid.UUID
    scenario: str = Field(max_length=64)
    mode: str = Field(default="frozen", max_length=16)
    seed: int = Field(default=7, ge=0, le=2**31 - 1)


def _view(r: ReplayRun) -> dict:
    return {"id": str(r.id), "source_incident_id": str(r.source_incident_id), "source_revision": r.source_revision,
            "scenario": r.scenario, "mode": r.mode, "seed": r.seed, "created_by": r.created_by, "created_at": r.created_at,
            "source": r.source_summary, "replay": r.replay_summary, "comparison": r.comparison, "trace": r.trace}


@router.get("/replays/scenarios")
def scenarios(p: Principal = Depends(principal)):
    require(p, "reviewer", "operator")
    return {"scenarios": replay.SCENARIOS, "modes": list(replay.MODES)}


@router.post("/replays")
def create_replay(req: ReplayRequest, p: Principal = Depends(principal), db: Session = Depends(get_db)):
    require(p, "reviewer")
    result = replay.run(db, req.incident_id, req.scenario, req.mode, req.seed)
    db.rollback()  # the simulator only reads; start a clean transaction for the record
    row = ReplayRun(id=result["run_id"], source_incident_id=req.incident_id, source_revision=result["source_revision"],
                    scenario=req.scenario, mode=req.mode, seed=req.seed, source_summary=result["source"],
                    replay_summary=result["replay"], comparison=result["comparison"], trace=result["trace"],
                    created_by=p.name)
    db.add(row)
    audit.record(db, "replay_run", p.name, incident_id=req.incident_id,
                 payload={"replay_id": str(row.id), "scenario": req.scenario, "mode": req.mode})
    db.commit()
    return _view(row)


@router.get("/replays")
def list_replays(incident_id: uuid.UUID, p: Principal = Depends(principal), db: Session = Depends(get_db)):
    require(p, "reviewer", "operator")
    rows = db.execute(select(ReplayRun).where(ReplayRun.source_incident_id == incident_id)
                      .order_by(ReplayRun.created_at.desc()).limit(50)).scalars()
    return [_view(r) for r in rows]


@router.get("/replays/{replay_id}/comparison")
def comparison(replay_id: uuid.UUID, p: Principal = Depends(principal), db: Session = Depends(get_db)):
    require(p, "reviewer", "operator")
    r = db.get(ReplayRun, replay_id)
    if r is None:
        raise NotFound("replay_not_found", str(replay_id))
    return _view(r)
