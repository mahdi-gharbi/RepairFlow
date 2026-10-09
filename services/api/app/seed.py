"""Idempotent seed of configuration tables (teams, equipment) from data/equipment/equipment.json."""
import json
import os
import pathlib

from sqlalchemy.dialects.postgresql import insert

from app.db import session
from app.models import Equipment, Team

DATA = pathlib.Path(os.environ.get("RF_DATA_DIR", "/data")) / "equipment" / "equipment.json"


def main() -> None:
    fixtures = json.loads(DATA.read_text(encoding="utf-8"))
    with session() as db:
        for t in fixtures["teams"]:
            db.execute(insert(Team).values(**t).on_conflict_do_update(index_elements=["id"], set_=t))
        for e in fixtures["equipment"]:
            db.execute(insert(Equipment).values(**e).on_conflict_do_update(index_elements=["id"], set_=e))
        db.commit()
    print(f"seeded {len(fixtures['teams'])} teams, {len(fixtures['equipment'])} equipment")


if __name__ == "__main__":
    main()
