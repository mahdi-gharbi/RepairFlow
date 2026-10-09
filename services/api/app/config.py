import os
from dataclasses import dataclass, field


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError(f"missing required environment variable {name}")
    return value


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default_factory=lambda: _env("RF_DATABASE_URL"))
    internal_key: str = field(default_factory=lambda: _env("RF_INTERNAL_KEY"))
    operator_token: str = field(default_factory=lambda: _env("RF_OPERATOR_TOKEN"))
    reviewer_token: str = field(default_factory=lambda: _env("RF_REVIEWER_TOKEN"))
    # Read-only lookups used by reconciliation (never used to create effects).
    ticket_service_url: str = field(default_factory=lambda: _env("RF_TICKET_SERVICE_URL", "http://ticket-service:8000"))
    ticket_api_key: str = field(default_factory=lambda: _env("TICKET_API_KEY", ""))
    mailpit_url: str = field(default_factory=lambda: _env("RF_MAILPIT_URL", "http://mailpit:8025"))
    # Telemetry older than this (relative to the report) is flagged as stale.
    telemetry_max_age_s: int = field(default_factory=lambda: int(_env("RF_TELEMETRY_MAX_AGE_S", "3600")))


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
