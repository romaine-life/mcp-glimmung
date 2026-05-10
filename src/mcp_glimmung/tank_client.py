"""Client for Tank's internal session test-state endpoint."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx

ORCHESTRATOR_URL = os.environ.get(
    "ORCHESTRATOR_INTERNAL_URL",
    "http://tank-operator.tank-operator.svc:80",
)
SA_TOKEN_PATH = os.environ.get(
    "SA_TOKEN_PATH",
    "/var/run/secrets/kubernetes.io/serviceaccount/token",
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
        sa_token_path: str = SA_TOKEN_PATH,
    ) -> None:
        self._url = orchestrator_url.rstrip("/")
        self._sa_token_path = Path(sa_token_path)

    def _sa_token(self) -> str:
        try:
            return self._sa_token_path.read_text().strip()
        except OSError as exc:
            raise RuntimeError(
                f"could not read SA token at {self._sa_token_path}: {exc}"
            ) from exc

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._sa_token()}"}

    def set_test_environment(
        self,
        caller_pod_ip: str,
        session_id: str,
        *,
        active: bool = True,
        slot_index: int | None = None,
        url: str | None = None,
        lease_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"active": active}
        if slot_index is not None:
            body["slot_index"] = slot_index
        if url:
            body["url"] = url
        if lease_id:
            body["lease_id"] = lease_id
        r = httpx.post(
            f"{self._url}/api/internal/sessions/{session_id}/test-state",
            params={"caller_pod_ip": caller_pod_ip},
            json=body,
            headers=self._headers(),
            timeout=15.0,
        )
        _check(r)
        return r.json()
