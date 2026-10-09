from app.db import session
from app.retrieval import MANUALS, index_manuals

with session() as db:
    print(f"indexed {index_manuals(db)} manual sections from {MANUALS}")
