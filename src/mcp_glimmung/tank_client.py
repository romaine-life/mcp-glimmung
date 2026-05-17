"""Client for Tank's internal session test-state endpoint.

Outbound auth is an auth.romaine.life-issued service-role JWT — same
exchanged token as GlimmungClient uses. tank-operator's verifier (see
backend-go/internal/auth/auth.go in nelsong6/tank-operator) accepts
role=service tokens with the same JWKS-based check, no per-app
TokenReview needed.
"""
from __future__ import annotations

import os
from typing import Any

import httpx

from .auth_exchange import AuthRomaineLifeExchangeClient, default_exchange_client

ORCHESTRATOR_URL = os.environ.get(
    "ORCHESTRATOR_INTERNAL_URL",
    "http://tank-operator.tank-operator.svc:80",
)

_ERROR_BODY_CAP = 1200


def _check(r: httpx.Response) -> None:
    if r.is_success:
        return
    body = r.text or ""
    if len(body) > _ERROR_BODY_CAP:
        body = body[:_ERROR_BODY_CAP] + "...(truncated)"
    detail = f": {body}" if body else ""
    raise httpx.HTTPStatusError(
        f"{r.status_code} {r.reason_phrase} for "
        f"{r.request.method} {r.request.url}{detail}",
        request=r.request,
        response=r,
    )


class TankClient:
    def __init__(
        self,
        orchestrator_url: str = ORCHESTRATOR_URL,
        exchange_client: AuthRomaineLifeExchangeClient | None = None,
    ) -> None:
        self._url = orchestrator_url.rstrip("/")
        self._exchange = exchange_client or default_exchange_client()

    def _headers(self) -> dict[str, str]:
        return self._exchange.bearer_header()

    def set_test_environment(
        self,
        caller_pod_ip: str,
        session_id: str,
        *,
        active: bool = True,
        slot_index: int | None = None,
        url: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"active": active}
        if slot_index is not None:
            body["slot_index"] = slot_index
        if url:
            body["url"] = url
        r = httpx.post(
            f"{self._url}/api/internal/sessions/{session_id}/test-state",
            params={"caller_pod_ip": caller_pod_ip},
            json=body,
            headers=self._headers(),
            timeout=15.0,
        )
        _check(r)
        return r.json()
