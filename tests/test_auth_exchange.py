"""Unit tests for the auth.romaine.life token-exchange client.

The verifier on the receiving side lives in glimmung and tank-operator;
this test suite covers exchange-side behavior: caching, refresh near
expiry, error surfacing, and the stub-server contract.
"""

from __future__ import annotations

import base64
import json
import threading
from pathlib import Path

import httpx
import pytest

from mcp_glimmung.auth_exchange import (
    AuthRomaineLifeExchangeClient,
    jwt_expiry_unsafe,
)


def _jwt_with_exp(exp_epoch: float) -> str:
    """Mint a syntactically-valid JWT for the test (signature not verified
    here — glimmung is the verifier)."""
    header = base64.urlsafe_b64encode(
        json.dumps({"alg": "RS256", "kid": "test"}).encode()
    ).rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": int(exp_epoch), "role": "service"}).encode()
    ).rstrip(b"=").decode()
    return f"{header}.{payload}.fakesig"


def _exchange_server(handler):
    """Build an httpx.MockTransport that calls `handler(request) -> (status, body_dict)`."""

    def respond(request: httpx.Request) -> httpx.Response:
        status, body = handler(request)
        return httpx.Response(status, json=body)

    return httpx.Client(transport=httpx.MockTransport(respond))


@pytest.fixture
def sa_token_path(tmp_path: Path) -> Path:
    p = tmp_path / "token"
    p.write_text("k8s-sa-token-bytes")
    return p


def test_jwt_returned_and_cached(sa_token_path: Path) -> None:
    now = [1_000.0]
    calls: list[dict] = []

    def handler(request: httpx.Request) -> tuple[int, dict]:
        # The auth.romaine.life /api/auth/exchange/k8s endpoint reads
        # the SA token from `Authorization: Bearer <sa_jwt>`. Record the
        # header + body so the test pins both halves of the contract:
        # the header carries the token, the body is empty (the route
        # ignores any body).
        calls.append(
            {
                "authorization": request.headers.get("Authorization"),
                "body": bytes(request.content),
            }
        )
        return 200, {
            "token": _jwt_with_exp(now[0] + 900),
            "expires_at": now[0] + 900,
            "userId": "svc:mcp-glimmung:mcp-glimmung",
            "email": "pod-mcp-glimmung@service.mcp-glimmung.romaine.life",
            "actorEmail": "pod-mcp-glimmung@service.mcp-glimmung.romaine.life",
            "sessionId": "mcp-glimmung",
        }

    client = AuthRomaineLifeExchangeClient(
        sa_token_path=sa_token_path,
        exchange_url="https://auth.romaine.life/api/auth/exchange/k8s",
        http_client=_exchange_server(handler),
        now_fn=lambda: now[0],
    )

    h1 = client.bearer_header()
    h2 = client.bearer_header()
    assert h1["Authorization"].startswith("Bearer ")
    assert h1 == h2
    # Cached: only one exchange call.
    assert len(calls) == 1
    # The SA token rides on the Authorization header, not in a body —
    # the endpoint contract is Bearer-only.
    assert calls[0]["authorization"] == "Bearer k8s-sa-token-bytes"
    assert calls[0]["body"] == b""


def test_refresh_when_near_expiry(sa_token_path: Path) -> None:
    now = [1_000.0]
    tokens = ["first", "second"]

    def handler(request: httpx.Request) -> tuple[int, dict]:
        token_label = tokens.pop(0)
        return 200, {
            "token": _jwt_with_exp(now[0] + 600),
            "expires_at": now[0] + 600,
            "label": token_label,  # not used by client; sanity-check the round-trip
            "userId": "svc:mcp-glimmung:mcp-glimmung",
            "email": "pod-mcp-glimmung@service.mcp-glimmung.romaine.life",
            "actorEmail": "pod-mcp-glimmung@service.mcp-glimmung.romaine.life",
            "sessionId": "mcp-glimmung",
        }

    client = AuthRomaineLifeExchangeClient(
        sa_token_path=sa_token_path,
        exchange_url="https://auth.romaine.life/api/auth/exchange/k8s",
        http_client=_exchange_server(handler),
        now_fn=lambda: now[0],
    )

    client.bearer_header()  # caches token, expires_at = 1600
    # Move past the leeway window (default 60s before expiry).
    now[0] = 1_550.0
    client.bearer_header()  # forces refresh
    assert tokens == []  # both refresh calls consumed


def test_exchange_failure_surfaces_clear_error(sa_token_path: Path) -> None:
    def handler(_request: httpx.Request) -> tuple[int, dict]:
        return 403, {"reason": "denied_allowlist"}

    client = AuthRomaineLifeExchangeClient(
        sa_token_path=sa_token_path,
        exchange_url="https://auth.romaine.life/api/auth/exchange/k8s",
        http_client=_exchange_server(handler),
        now_fn=lambda: 1_000.0,
    )

    with pytest.raises(RuntimeError, match="exchange failed: HTTP 403"):
        client.jwt()


def test_missing_sa_token_file_surfaces_clear_error(tmp_path: Path) -> None:
    client = AuthRomaineLifeExchangeClient(
        sa_token_path=tmp_path / "does-not-exist",
        exchange_url="https://auth.romaine.life/api/auth/exchange/k8s",
        http_client=_exchange_server(lambda r: (200, {})),
        now_fn=lambda: 1_000.0,
    )
    with pytest.raises(RuntimeError, match="could not read auth.romaine.life SA token"):
        client.jwt()


def test_empty_sa_token_file_surfaces_clear_error(tmp_path: Path) -> None:
    empty = tmp_path / "token"
    empty.write_text("")
    client = AuthRomaineLifeExchangeClient(
        sa_token_path=empty,
        exchange_url="https://auth.romaine.life/api/auth/exchange/k8s",
        http_client=_exchange_server(lambda r: (200, {})),
        now_fn=lambda: 1_000.0,
    )
    with pytest.raises(RuntimeError, match="is empty"):
        client.jwt()


def test_concurrent_refresh_shares_one_call(sa_token_path: Path) -> None:
    now = [1_000.0]
    calls = 0
    barrier = threading.Barrier(2)

    def handler(_request: httpx.Request) -> tuple[int, dict]:
        nonlocal calls
        calls += 1
        # Hold inside the handler so both threads contend on the lock.
        barrier.wait(timeout=2)
        return 200, {
            "token": _jwt_with_exp(now[0] + 900),
            "expires_at": now[0] + 900,
            "userId": "svc:mcp-glimmung:mcp-glimmung",
            "email": "pod-mcp-glimmung@service.mcp-glimmung.romaine.life",
            "actorEmail": "pod-mcp-glimmung@service.mcp-glimmung.romaine.life",
            "sessionId": "mcp-glimmung",
        }

    client = AuthRomaineLifeExchangeClient(
        sa_token_path=sa_token_path,
        exchange_url="https://auth.romaine.life/api/auth/exchange/k8s",
        http_client=_exchange_server(handler),
        now_fn=lambda: now[0],
    )

    results: list[str] = []

    def worker():
        results.append(client.jwt())

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    # Give t1 a moment to enter the lock before t2 starts.
    barrier.wait(timeout=2)
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    # Both threads should have returned the same token, and the exchange
    # endpoint should have been hit exactly once (the second caller
    # observed the cache populated by the first).
    assert results[0] == results[1]
    assert calls == 1


def test_jwt_expiry_unsafe_decodes() -> None:
    token = _jwt_with_exp(1_700_000_000)
    assert jwt_expiry_unsafe(token) == 1_700_000_000.0


def test_jwt_expiry_unsafe_handles_malformed() -> None:
    assert jwt_expiry_unsafe("only.two") is None
    assert jwt_expiry_unsafe("not-a-jwt") is None
