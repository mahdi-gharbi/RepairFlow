"""Lexical evidence retrieval over the fictional manuals (PostgreSQL full-text, english config).

Filtering by equipment model and excluding superseded versions happens *before* ranking. A rank score is
a retrieval signal, not a technical confidence, and is never shown as one.

Known limit: reports are often in French while manuals are in English; lexical search does not bridge
languages. The query is therefore built from language-neutral keys (telemetry error codes) plus a fixed
English expansion of the rules-engine category, plus the report's own words.
"""
import json
import os
import pathlib
import re
from datetime import date

from sqlalchemy import case, func, literal_column, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.canonical import sha256_hex
from app.models import RetrievalChunk

MANUALS = pathlib.Path(os.environ.get("RF_DATA_DIR", "/data")) / "manuals"
EXPANSION = {
    "battery": "battery power shutdown charge",
    "network": "network wifi offline connectivity",
    "mechanical": "wheel motor vibration grinding drive",
    "sensor": "sensor camera calibration detection",
    "other": "",
}
TOP_K = 3
# An exact, language-neutral error-code match outranks any amount of keyword overlap.
# (Adjusted after observing E-NET-03 ranked below a wifi-heavy section; tuned on dev cases only.)
ERROR_CODE_BOOST = 1.0


def parse_manual(path: pathlib.Path) -> tuple[dict, list[dict]]:
    raw = path.read_text(encoding="utf-8")
    _, front, body = raw.split("---", 2)
    meta = json.loads(front)
    chunks = []
    for m in re.finditer(r"^## (.+?)\n(.*?)(?=^## |\Z)", body, flags=re.S | re.M):
        chunks.append({"section": m.group(1).strip(), "text": m.group(2).strip(),
                       "span_start": m.start(), "span_end": m.end()})
    return meta, chunks


def index_manuals(db: Session, directory: pathlib.Path = MANUALS) -> int:
    """Idempotent: upserts every section; marks a version superseded when a newer one exists."""
    docs = [parse_manual(p) for p in sorted(directory.glob("*.md")) if p.name != "README.md"]
    latest: dict[str, tuple] = {}
    for meta, _ in docs:
        key = tuple(int(x) for x in meta["version"].split("."))
        latest[meta["source_id"]] = max(latest.get(meta["source_id"], key), key)
    count = 0
    for meta, chunks in docs:
        superseded = bool(meta.get("superseded_by")) or tuple(int(x) for x in meta["version"].split(".")) < latest[meta["source_id"]]
        for n, c in enumerate(chunks, 1):
            row = {"id": f"{meta['source_id']}@{meta['version']}#{n}", "source_id": meta["source_id"], "version": meta["version"],
                   "published_at": date.fromisoformat(meta["published_at"]), "title": meta["title"], "section": c["section"],
                   "span_start": c["span_start"], "span_end": c["span_end"], "text": c["text"], "models": meta["models"],
                   "superseded": superseded, "content_hash": sha256_hex(c["text"])}
            db.execute(insert(RetrievalChunk).values(**row).on_conflict_do_update(index_elements=["id"], set_=row))
            count += 1
    db.commit()
    return count


def build_query(category: str, report_text: str, error_code: str | None) -> str:
    words = re.findall(r"[A-Za-z][A-Za-z-]{3,}", report_text)
    terms = ([error_code] if error_code else []) + EXPANSION.get(category, "").split() + words
    terms = [t for t in dict.fromkeys(t.lower() for t in terms) if t]
    return " OR ".join(terms)


def retrieve(db: Session, equipment_model: str, query: str, k: int = TOP_K,
             error_code: str | None = None) -> list[tuple[RetrievalChunk, float]]:
    if not query:
        return []
    english = literal_column("'english'::regconfig")
    tsq = func.websearch_to_tsquery(english, query)
    rank = func.ts_rank_cd(RetrievalChunk.tsv, tsq)
    if error_code:
        rank = rank + case((RetrievalChunk.tsv.op("@@")(func.phraseto_tsquery(english, error_code)), ERROR_CODE_BOOST), else_=0.0)
    rows = db.execute(
        select(RetrievalChunk, rank)
        .where(RetrievalChunk.models.any(equipment_model), RetrievalChunk.superseded.is_(False),
               RetrievalChunk.tsv.op("@@")(tsq))
        .order_by(rank.desc(), RetrievalChunk.id).limit(k)
    ).all()
    return [(c, float(r)) for c, r in rows]

