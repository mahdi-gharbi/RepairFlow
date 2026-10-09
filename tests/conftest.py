import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "services" / "api"))


def _load_env() -> dict:
    env = {}
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


ENV = _load_env()
os.environ.setdefault("RF_INTERNAL_KEY", ENV.get("RF_INTERNAL_KEY", "unit-internal"))
os.environ.setdefault("RF_OPERATOR_TOKEN", ENV.get("RF_OPERATOR_TOKEN", "unit-operator"))
os.environ.setdefault("RF_REVIEWER_TOKEN", ENV.get("RF_REVIEWER_TOKEN", "unit-reviewer"))
os.environ.setdefault("TICKET_API_KEY", ENV.get("TICKET_API_KEY", ""))
# Integration tests talk to the compose PostgreSQL through its loopback port.
os.environ.setdefault("RF_DATABASE_URL",
                      f"postgresql+psycopg://repairflow:{ENV.get('RF_DB_PASSWORD', 'x')}@127.0.0.1:15432/repairflow")
os.environ.setdefault("RF_TICKET_SERVICE_URL", "http://127.0.0.1:18081")
os.environ.setdefault("RF_MAILPIT_URL", "http://127.0.0.1:18025")
