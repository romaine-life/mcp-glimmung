"""Client for Tank's internal session test-state endpoint.

Outbound auth forwards the inbound caller's auth.romaine.life JWT —
tank-operator's RomaineLifeJWTVerifier accepts the same JWKS-backed
service tokens, so the caller's actor_email rides through end-to-end.
"""
from __future__ import annotations

import os
from typing import Any

import httpx

from romaine_auth import current_caller

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
    ) -> None:
        self._url = orchestrator_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        caller = current_caller()
        if caller is None:
            raise RuntimeError(
                "no current_caller() bound; "
                "CallerJWTMiddleware should have 401'd this request"
            )
        return {"Authorization": f"Bearer {caller.raw_token}"}

    def set_test_environment(
        self,
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
            json=body,
            headers=self._headers(),
            timeout=15.0,
        )
        _check(r)
        return r.json()

    def upload_session_file(
        self,
        session_id: str,
        *,
        name: str,
        content_type: str,
        data: bytes,
    ) -> dict[str, Any]:
        """Upload bytes into a Tank session workspace.

        Tank's raw upload endpoint stores image content under
        `/workspace/screenshots/<n>.<ext>` and returns both relative and
        absolute paths. The caller session id is trusted context from the
        mcp-auth-proxy; callers cannot use this helper to target another
        workspace unless Tank's own ownership checks allow it.
        """
        r = httpx.post(
            f"{self._url}/api/sessions/{session_id}/files/upload",
            params={"name": name},
            content=data,
            headers={
                **self._headers(),
                "Content-Type": content_type,
            },
            timeout=30.0,
        )
        _check(r)
        return r.json()
