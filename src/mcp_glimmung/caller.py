"""Per-request caller pod IP extraction for Tank internal callbacks."""
from __future__ import annotations

from contextvars import ContextVar

CALLER_POD_IP: ContextVar[str | None] = ContextVar(
    "mcp_glimmung_caller_pod_ip", default=None
)


def current_caller_pod_ip() -> str | None:
    return CALLER_POD_IP.get()


def extract_source_pod_ip(forwarded_for: str | None, peer_ip: str | None) -> str | None:
    """Pick the session pod IP from kube-rbac-proxy's X-Forwarded-For chain."""
    if forwarded_for:
        last = forwarded_for.split(",")[-1].strip()
        if last:
            return last
    return peer_ip
