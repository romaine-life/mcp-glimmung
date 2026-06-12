"""GlimmungClient header composition.

The outbound Authorization header forwards the caller's JWT (delegated
auth); X-Glimmung-Actor refines attribution with the Tank session the
mcp-auth-proxy bound for this request, so glimmung's workflow control
ledger reads "svc:... via tank-session:815" instead of a bare subject.
"""
from __future__ import annotations

from types import SimpleNamespace

import mcp_glimmung.glimmung_client as glimmung_client
from mcp_glimmung.caller import CALLER_SESSION_ID
from mcp_glimmung.glimmung_client import ACTOR_HEADER, GlimmungClient


def test_headers_forward_actor_when_session_bound(monkeypatch) -> None:
    monkeypatch.setattr(
        glimmung_client,
        "current_caller",
        lambda: SimpleNamespace(raw_token="jwt-token", sub="svc:tank:815"),
    )
    client = GlimmungClient(base_url="http://glimmung.test")

    token = CALLER_SESSION_ID.set("session-815")
    try:
        headers = client._headers()
    finally:
        CALLER_SESSION_ID.reset(token)

    assert headers["Authorization"] == "Bearer jwt-token"
    assert headers[ACTOR_HEADER] == "tank-session:815"


def test_headers_omit_actor_without_session(monkeypatch) -> None:
    monkeypatch.setattr(
        glimmung_client,
        "current_caller",
        lambda: SimpleNamespace(raw_token="jwt-token", sub="user@romaine.life"),
    )
    client = GlimmungClient(base_url="http://glimmung.test")

    token = CALLER_SESSION_ID.set(None)
    try:
        headers = client._headers()
    finally:
        CALLER_SESSION_ID.reset(token)

    assert headers["Authorization"] == "Bearer jwt-token"
    assert ACTOR_HEADER not in headers
