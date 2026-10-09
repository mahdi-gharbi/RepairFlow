"""Lexical retrieval over the fictional manuals (real PostgreSQL full-text index)."""
import shutil

import httpx
import pytest
from sqlalchemy import delete

from app import retrieval
from app.db import session
from app.models import RetrievalChunk
from conftest import ROOT
from helpers import API, INTERNAL, REVIEWER, approve, compose, incident, ready_incident

MANUALS = ROOT / "data" / "manuals"


def top(model, category, text, code=None):
    with session() as db:
        return [c.id for c, _ in retrieval.retrieve(db, model, retrieval.build_query(category, text, code), error_code=code)]


def test_error_code_ranks_the_matching_section_first():
    assert top("AMR-200", "battery", "Le robot s'arrête", "E-PWR-02")[0] == "man-amr-power@2.1#2"
    assert top("AMR-300", "network", "stops receiving missions", "E-NET-03")[0] == "man-amr-network@1.4#2"
    assert top("ARM-6", "other", "missing parcels picks off-center", "E-SNS-07")[0] == "man-arm6-sensors@3.0#2"


def test_superseded_versions_are_never_returned():
    with session() as db:
        assert db.get(RetrievalChunk, "man-amr-power@1.0#2").superseded
    assert not [i for i in top("AMR-200", "battery", "shutdown fuses motor", "E-PWR-02") if "@1.0" in i]


def test_model_filter_applies_before_ranking():
    assert all(not i.startswith(("man-amr-power", "man-amr-network")) for i in top("ARM-6", "battery", "battery shutdown"))
    assert all(not i.startswith("man-arm6") for i in top("AMR-200", "sensor", "camera calibration detection"))


def test_manual_evidence_is_snapshotted_and_idempotent():
    iid = ready_incident()
    first = httpx.post(f"{API}/v1/incidents/{iid}/evidence/manuals", headers=INTERNAL, timeout=30).json()
    assert first["changed"] and first["hits"][0]["chunk_id"] == "man-amr-power@2.1#2"
    again = httpx.post(f"{API}/v1/incidents/{iid}/evidence/manuals", headers=INTERNAL, timeout=30).json()
    assert again["changed"] is False and again["revision"] == first["revision"]
    kinds = [e["kind"] for e in incident(iid)["evidence"]]
    assert kinds.count("manual") == len(first["hits"])


@pytest.fixture
def publish_newer_power_manual(tmp_path):
    """Returns publish(): indexes v2.2 of the power manual in the shared index. Teardown restores the corpus."""
    def publish():
        text = (MANUALS / "amr-power-v2.md").read_text(encoding="utf-8")
        text = text.replace('"version": "2.1", "published_at": "2026-03-02"', '"version": "2.2", "published_at": "2026-10-09"')
        text = text.replace("Check battery_pct in telemetry before anything else;",
                            "Since firmware 4.2, first check the BMS log for cell imbalance, then battery_pct;")
        for f in MANUALS.glob("*.md"):
            shutil.copy(f, tmp_path / f.name)
        (tmp_path / "amr-power-v2.2.md").write_text(text, encoding="utf-8")
        with session() as db:
            retrieval.index_manuals(db, tmp_path)
    yield publish
    with session() as db:
        db.execute(delete(RetrievalChunk).where(RetrievalChunk.source_id == "man-amr-power", RetrievalChunk.version == "2.2"))
        db.commit()
        retrieval.index_manuals(db, MANUALS)


def test_new_manual_version_after_approval_invalidates_it(publish_newer_power_manual):
    compose("stop", "worker")  # keep the approved actions from being dispatched during the check
    try:
        iid = ready_incident()
        httpx.post(f"{API}/v1/incidents/{iid}/evidence/manuals", headers=INTERNAL, timeout=30)
        d = incident(iid)
        assert {e["source_version"] for e in d["evidence"] if e["kind"] == "manual"} == {"2.1"}
        assert approve(iid).status_code == 200
        publish_newer_power_manual()  # v2.2 published after the approval
        r = httpx.post(f"{API}/v1/incidents/{iid}/evidence/manuals", headers=INTERNAL, timeout=30).json()
        assert r["changed"]
        d = incident(iid)
        assert d["status"] == "needs_review"
        assert {a["status"] for a in d["actions"]} == {"cancelled"}
        assert "2.2" in {e["source_version"] for e in d["evidence"] if e["kind"] == "manual"}
        stale = httpx.post(f"{API}/v1/incidents/{iid}/approve", headers=REVIEWER,
                           json={"expected_revision": d["revision"] - 1, "plan_digest": "0" * 64})
        assert stale.status_code == 409 and stale.json()["error"] == "stale_revision"
    finally:
        compose("start", "worker")
