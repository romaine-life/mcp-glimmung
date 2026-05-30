"""HTTP client for glimmung.

Outbound auth forwards the inbound caller's auth.romaine.life JWT —
glimmung verifies against the same JWKS, so the caller's actor_email
rides through the call chain end-to-end without any per-app re-mint.

The current caller is bound by CallerJWTMiddleware in http.py and read
back here via romaine_auth.current_caller(). Hitting None means a
request reached a tool handler without going through the middleware,
which is a bug — the middleware requires the JWT on every non-/healthz
path.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from romaine_auth import current_caller

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://glimmung.glimmung.svc"


class GlimmungClient:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._http = httpx.Client(timeout=timeout)

    def _headers(self) -> dict[str, str]:
        caller = current_caller()
        if caller is None:
            raise RuntimeError(
                "no current_caller() bound; "
                "CallerJWTMiddleware should have 401'd this request"
            )
        return {"Authorization": f"Bearer {caller.raw_token}"}

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        r = self._http.get(self._base_url + path, params=params, headers=self._headers())
        r.raise_for_status()
        return r.json()

    def patch(self, path: str, json: dict[str, Any]) -> Any:
        r = self._http.patch(self._base_url + path, json=json, headers=self._headers())
        r.raise_for_status()
        return r.json()

    def delete(self, path: str, params: dict[str, Any] | None = None) -> Any:
        r = self._http.delete(self._base_url + path, params=params, headers=self._headers())
        r.raise_for_status()
        return r.json()

    def post(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        r = self._http.post(
            self._base_url + path,
            params=params,
            json=json,
            headers=self._headers(),
        )
        r.raise_for_status()
        return r.json()

    def post_multipart(
        self,
        path: str,
        *,
        data: dict[str, str] | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        """POST a multipart/form-data request to glimmung.

        ``files`` is the same shape `httpx` accepts: a dict of
        ``part_name -> (filename, bytes, content_type)``. ``data`` is the
        plain form fields. ``extra_headers`` layers on top of the standard
        auth header so the caller can ship custom headers such as
        ``X-Inspection-Request-Id`` without re-implementing the auth
        plumbing.
        """
        headers = self._headers()
        if extra_headers:
            for k, v in extra_headers.items():
                headers[k] = v
        r = self._http.post(
            self._base_url + path,
            data=data,
            files=files,
            headers=headers,
        )
        r.raise_for_status()
        return r.json()
