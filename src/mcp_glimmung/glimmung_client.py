"""HTTP client for glimmung.

Outbound auth is an auth.romaine.life-issued service-role JWT obtained
by exchanging the pod's projected SA token (audience
`https://auth.romaine.life`) at the IdP's exchange endpoint. Glimmung
verifies the JWT against auth.romaine.life's JWKS — same trust root
as every other relying party in the .romaine.life ecosystem.

The JWT is cached and refreshed near expiry by AuthRomaineLifeExchangeClient;
mcp-glimmung doesn't re-exchange on every call.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .auth_exchange import AuthRomaineLifeExchangeClient, default_exchange_client

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://glimmung.glimmung.svc"


class GlimmungClient:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
        exchange_client: AuthRomaineLifeExchangeClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._http = httpx.Client(timeout=timeout)
        self._exchange = exchange_client or default_exchange_client()

    def _headers(self) -> dict[str, str]:
        return self._exchange.bearer_header()

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        r = self._http.get(self._base_url + path, params=params, headers=self._headers())
        r.raise_for_status()
        return r.json()

    def patch(self, path: str, json: dict[str, Any]) -> Any:
        r = self._http.patch(self._base_url + path, json=json, headers=self._headers())
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
