"""Server-side authentication. Two local demo users (operator, reviewer) with bearer tokens, plus a
distinct header secret for internal callers (n8n workflows, worker). The UI never decides a role."""
import secrets
from dataclasses import dataclass

from fastapi import Header

from app.config import get_settings
from app.errors import DomainError


class Unauthorized(DomainError):
    status = 401


class ForbiddenRole(DomainError):
    status = 403


@dataclass(frozen=True)
class Principal:
    name: str
    role: str  # operator | reviewer | internal


def _match(given: str | None, expected: str) -> bool:
    return bool(given) and bool(expected) and secrets.compare_digest(given.encode(), expected.encode())


def principal(authorization: str | None = Header(default=None),
              x_repairflow_internal: str | None = Header(default=None)) -> Principal:
    s = get_settings()
    if x_repairflow_internal is not None:
        if _match(x_repairflow_internal, s.internal_key):
            return Principal("n8n", "internal")
        raise Unauthorized("bad_internal_key")
    if authorization and authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ")
        if _match(token, s.reviewer_token):
            return Principal("reviewer-demo", "reviewer")
        if _match(token, s.operator_token):
            return Principal("operator-demo", "operator")
    raise Unauthorized("authentication_required")


def require(p: Principal, *roles: str) -> Principal:
    if p.role not in roles:
        raise ForbiddenRole("forbidden", f"role {p.role} cannot perform this operation")
    return p
