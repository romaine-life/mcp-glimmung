"""Per-request caller context for Tank/Glimmung tool calls."""
from __future__ import annotations

from contextvars import ContextVar

from romaine_auth import current_caller

CALLER_SESSION_ID: ContextVar[str | None] = ContextVar(
    "mcp_glimmung_caller_session_id", default=None
)
CALLER_SESSION_SCOPE: ContextVar[str | None] = ContextVar(
    "mcp_glimmung_caller_session_scope", default=None
)

CALLER_SYSTEM_HEADER = "x-tank-caller-system"
CALLER_KIND_HEADER = "x-tank-caller-kind"
CALLER_SESSION_ID_HEADER = "x-tank-caller-session-id"
CALLER_SESSION_SCOPE_HEADER = "x-tank-caller-session-scope"


def current_tank_session_id() -> str | None:
    session_id = _normalize_session_id(CALLER_SESSION_ID.get())
    if session_id:
        return session_id

    caller = current_caller()
    sub = str(getattr(caller, "sub", "") or "")
    prefix = "svc:tank:"
    if sub.startswith(prefix):
        return _normalize_session_id(sub[len(prefix) :])
    return None


def current_tank_session_scope() -> str | None:
    scope = str(CALLER_SESSION_SCOPE.get() or "").strip()
    if scope:
        return scope
    if current_tank_session_id():
        return "default"
    return None


def require_tank_session_id() -> str:
    session_id = current_tank_session_id()
    if not session_id:
        raise ValueError(
            "trusted Tank caller session identity is required; mcp-auth-proxy "
            "must send X-Tank-Caller-Session-Id or the verified caller JWT "
            "must use sub=svc:tank:<session-id>"
        )
    return session_id


def _normalize_session_id(value: str | None) -> str | None:
    normalized = str(value or "").strip().removeprefix("session-")
    return normalized or None
