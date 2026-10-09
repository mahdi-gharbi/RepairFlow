#!/bin/sh
set -e
alembic upgrade head
python -m app.seed
python -m app.retrieval
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --no-access-log
