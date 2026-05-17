"""Exchange a projected K8s SA token for an auth.romaine.life service-role JWT.

mcp-glimmung is one of several relying parties in the .romaine.life
ecosystem. Outbound calls to glimmung and tank-operator used to present
the pod's K8s SA token directly, which both services validated via
TokenReview against the cluster API. That worked but deviated from the
platform's "auth.romaine.life is the single identity provider" stance —
every other caller (browser, CLI bot, tank-operator session pod) now
goes through an auth.romaine.life-issued JWT.

This module closes the gap. At startup (and at refresh time) it reads
the pod's projected SA token mounted with audience
`https://auth.romaine.life`, POSTs it to
`https://auth.romaine.life/api/auth/exchange/k8s`, and returns the
auth.romaine.life-signed JWT. Glimmung and tank-operator both verify
that JWT against auth.romaine.life's JWKS — same trust root as every
other caller in the platform.

The JWT is cached for slightly less than its lifetime (90% of the
remaining time, capped at the configured leeway). Concurrent calls
during a refresh window share a single in-flight refresh via a lock so
the exchange endpoint doesn't see a thundering herd on startup or
after a key rotation.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx

log = logging.getLogger(__name__)

# Default mount path for the auth.romaine.life-audience projected SA
# token. The chart configures this; see
# chart/templates/deployment.yaml.
DEFAULT_AUTH_ROMAINE_LIFE_SA_TOKEN_PATH = Path(
    "/var/run/secrets/auth.romaine.life/token"
)
DEFAULT_AUTH_ROMAINE_LIFE_EXCHANGE_URL = (
    "https://auth.romaine.life/api/auth/exchange/k8s"
)

# Refresh the cached JWT this many seconds before its `exp` claim. Token
# lifetime from the IdP defaults to 15 minutes; we want to overlap the
# refresh window with the previous token so a slow exchange doesn't
# cause a request-time 401.
_REFRESH_LEEWAY_SECONDS = 60


@dataclass(frozen=True)
class _CachedJWT:
    token: str
    expires_at_epoch: float


class AuthRomaineLifeExchangeClient:
    """Caches an exchanged JWT for outbound auth to glimmung/tank-operator.

    Thread-safe; the bearer header builder used by client classes can be
    called concurrently from any request. A single in-flight refresh is
    enforced by the lock so concurrent callers during a refresh window
    share one network round-trip to the IdP.
    """

    def __init__(
        self,
        *,
        sa_token_path: Path = DEFAULT_AUTH_ROMAINE_LIFE_SA_TOKEN_PATH,
        exchange_url: str = DEFAULT_AUTH_ROMAINE_LIFE_EXCHANGE_URL,
        http_client: httpx.Client | None = None,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self._sa_token_path = Path(sa_token_path)
        self._exchange_url = exchange_url
        self._http = http_client or httpx.Client(timeout=10.0)
        self._now = now_fn
        self._lock = threading.Lock()
        self._cached: _CachedJWT | None = None

    def jwt(self) -> str:
        """Return a fresh-enough JWT, refreshing if past the leeway window."""
        cached = self._cached
        if cached is not None and self._now() < cached.expires_at_epoch - _REFRESH_LEEWAY_SECONDS:
            return cached.token
        with self._lock:
            cached = self._cached
            if cached is not None and self._now() < cached.expires_at_epoch - _REFRESH_LEEWAY_SECONDS:
                return cached.token
            fresh = self._exchange()
            self._cached = fresh
            return fresh.token

    def bearer_header(self) -> dict[str, str]:
        """Return the bearer header for outbound calls.

        Prefers forwarding the **inbound caller's JWT** when one is
        present on the current request (set by CallerJWTMiddleware via
        the CALLER ContextVar). Forwarding preserves the caller's
        actor_email — glimmung's audit log records the originating
        human, not the pod-stable mcp-glimmung identity.

        Falls back to the **pod-stable exchanged JWT** when there's no
        inbound caller — e.g., internal warmup paths, background tasks,
        any caller that didn't present a JWT (kube-rbac-proxy still
        gated connectivity, but identity is "the pod" not "a user").
        """
        # Defer the import to dodge an auth_verifier → auth_exchange
        # cycle at module load time; both import each other through
        # the http.py glue.
        from .auth_verifier import current_caller

        caller = current_caller()
        if caller is not None:
            return {"Authorization": f"Bearer {caller.raw_token}"}
        return {"Authorization": f"Bearer {self.jwt()}"}

    def _exchange(self) -> _CachedJWT:
        # The auth.romaine.life /api/auth/exchange/k8s endpoint reads the
        # K8s SA token from `Authorization: Bearer <jwt>`, not from a
        # JSON body. Sending the token in the body produced silent 401
        # `{"error":"missing bearer token"}` responses because the route
        # never inspects the body — the contract is documented in
        # nelsong6/auth `src/server.ts` (the handler that emits the
        # "missing bearer token" 401 when the Bearer header is absent).
        sa_token = self._read_sa_token()
        r = self._http.post(
            self._exchange_url,
            headers={"Authorization": f"Bearer {sa_token}"},
        )
        if r.status_code != 200:
            body = r.text[:400] if r.text else ""
            raise RuntimeError(
                f"auth.romaine.life exchange failed: HTTP {r.status_code} {body!r}"
            )
        payload = r.json()
        token = payload.get("token")
        expires_at = payload.get("expires_at")
        if not isinstance(token, str) or not isinstance(expires_at, (int, float)):
            raise RuntimeError(
                f"auth.romaine.life exchange returned malformed payload: {payload!r}"
            )
        log.info(
            "auth.romaine.life exchange ok: expires_in=%.0fs",
            float(expires_at) - self._now(),
        )
        return _CachedJWT(token=token, expires_at_epoch=float(expires_at))

    def _read_sa_token(self) -> str:
        try:
            raw = self._sa_token_path.read_text().strip()
        except OSError as exc:
            raise RuntimeError(
                f"could not read auth.romaine.life SA token at {self._sa_token_path}: {exc}"
            ) from exc
        if not raw:
            raise RuntimeError(
                f"auth.romaine.life SA token at {self._sa_token_path} is empty; "
                "is the projected token volume mounted with the right audience?"
            )
        return raw


def default_exchange_client() -> AuthRomaineLifeExchangeClient:
    """Construct an exchange client from environment-driven config.

    Environment knobs (test-only — production uses the defaults):
      - AUTH_ROMAINE_LIFE_SA_TOKEN_PATH overrides the SA token mount.
      - AUTH_ROMAINE_LIFE_EXCHANGE_URL overrides the exchange endpoint.
    """
    sa_path = Path(
        os.environ.get(
            "AUTH_ROMAINE_LIFE_SA_TOKEN_PATH",
            str(DEFAULT_AUTH_ROMAINE_LIFE_SA_TOKEN_PATH),
        )
    )
    exchange_url = os.environ.get(
        "AUTH_ROMAINE_LIFE_EXCHANGE_URL",
        DEFAULT_AUTH_ROMAINE_LIFE_EXCHANGE_URL,
    )
    return AuthRomaineLifeExchangeClient(
        sa_token_path=sa_path,
        exchange_url=exchange_url,
    )


def jwt_expiry_unsafe(jwt_token: str) -> float | None:
    """Decode the `exp` claim from a JWT without verifying its signature.

    Test-only helper for the exchange client's tests — the real verifier
    lives in glimmung. Returns None if the token isn't decodable.
    """
    parts = jwt_token.split(".")
    if len(parts) != 3:
        return None
    body = parts[1]
    body += "=" * (-len(body) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(body.encode()))
    except (ValueError, json.JSONDecodeError):
        return None
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)):
        return None
    return float(exp)
