"""HTTP entrypoint — streamable-http transport, required JWT auth.

Two layers gate inbound calls:

  1. **kube-rbac-proxy** (in front of this process): validates the
     caller's K8s SA token via TokenReview + SubjectAccessReview.
     "Some pod with an allowed SA is talking to me" — no human
     identity. Binding loopback so direct pod-IP:8080 access bypasses
     nothing.

  2. **auth.romaine.life JWT** (in this process): the caller's
     auth.romaine.life-issued JWT arrives in the
     ``X-Auth-Romaine-Token`` header (kube-rbac-proxy consumes
     ``Authorization`` for its own TokenReview and strips it before
     forwarding upstream, so the JWT rides on a separate header).
     ``CallerJWTMiddleware`` verifies the JWT against the IdP's JWKS
     and binds the resolved Caller (sub, email, role, actor_email) to
     a ContextVar. Tool handlers attribute their work to a specific
     human via Caller.display_actor.

     The header is REQUIRED on every non-``/healthz`` path. Missing
     or invalid → 401. There is no fallback to a synthetic identity;
     mcp-auth-proxy in session pods always injects the header, and
     anything that reaches this surface without it is unattributed
     and refused.

Outbound auth to glimmung / tank-operator: forwards the inbound
caller's raw JWT. Same trust root (auth.romaine.life JWKS) on the
receiving end, so the actor_email chain rides through end-to-end
without any per-app re-minting.
"""

import logging
import os
from contextlib import asynccontextmanager

import jwt
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

from romaine_auth import (
    CALLER,
    AuthRomaineLifeVerifier,
    default_verifier,
    warn_jwks_unreachable,
)

from .caller import CALLER_POD_IP, extract_source_pod_ip
from .glimmung_client import GlimmungClient
from .tank_client import TankClient
from .tools import register_tools

log = logging.getLogger(__name__)

# Header name shared with mcp-tank-operator and mcp-auth-proxy. The
# inbound auth.romaine.life service JWT rides on this header because
# kube-rbac-proxy strips Authorization. Changing it requires a
# cross-repo coordinated deploy (mcp-auth-proxy in tank-operator
# injects the same name).
CALLER_JWT_HEADER = "X-Auth-Romaine-Token"


class CallerPodIPMiddleware(BaseHTTPMiddleware):
    """Extract caller pod IP from X-Forwarded-For and bind to ContextVar."""

    async def dispatch(self, request: Request, call_next):
        forwarded_for = request.headers.get("x-forwarded-for")
        peer_ip = request.client.host if request.client else None
        pod_ip = extract_source_pod_ip(forwarded_for, peer_ip)
        token = CALLER_POD_IP.set(pod_ip)
        try:
            return await call_next(request)
        finally:
            CALLER_POD_IP.reset(token)


class CallerJWTMiddleware(BaseHTTPMiddleware):
    """Verify the X-Auth-Romaine-Token JWT against auth.romaine.life's
    JWKS and bind the resolved Caller to a ContextVar.

    The header is required on every non-/healthz path — missing or
    invalid → 401. No fallback to a synthetic identity; every tool
    call must be attributable to a real caller.
    """

    _BYPASS_PATHS = frozenset({"/healthz"})

    def __init__(self, app, verifier: AuthRomaineLifeVerifier):
        super().__init__(app)
        self._verifier = verifier

    async def dispatch(self, request: Request, call_next):
        if request.url.path in self._BYPASS_PATHS:
            return await call_next(request)

        token = request.headers.get(CALLER_JWT_HEADER, "").strip()
        if not token:
            return JSONResponse(
                {"error": f"missing {CALLER_JWT_HEADER} header"},
                status_code=401,
            )

        try:
            caller = self._verifier.verify(token)
        except (jwt.PyJWTError, ValueError) as exc:
            log.info("inbound JWT verification failed: %s", exc)
            return JSONResponse(
                {"error": "invalid auth.romaine.life JWT", "detail": str(exc)},
                status_code=401,
            )
        except Exception as exc:
            warn_jwks_unreachable(
                os.environ.get("AUTH_ROMAINE_LIFE_JWKS_URL", "<default>"), exc
            )
            return JSONResponse(
                {"error": "JWKS unreachable; cannot verify inbound JWT"},
                status_code=503,
            )

        token_ctx = CALLER.set(caller)
        try:
            return await call_next(request)
        finally:
            CALLER.reset(token_ctx)


def build_app() -> Starlette:
    # streamable_http ships DNS-rebinding-protection middleware that 421s any
    # Host header not in `allowed_hosts`. Default whitelist only covers
    # localhost, so in-cluster requests to mcp-glimmung.mcp-glimmung.svc get
    # rejected. Disable here — kube-rbac-proxy in front already gates auth
    # via K8s SA tokens. streamable_http_path="/" so POSTs to "/" don't hit
    # Starlette's trailing-slash redirect (was 307 → 421 loop in mcp-github).
    mcp = FastMCP(
        "glimmung-mcp",
        stateless_http=True,
        streamable_http_path="/",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        ),
    )
    base_url = os.environ.get("GLIMMUNG_BASE_URL", "http://glimmung.glimmung.svc")
    register_tools(mcp, GlimmungClient(base_url=base_url), TankClient())

    async def healthz(_: Request) -> Response:
        return Response("ok", media_type="text/plain")

    async def delete_session(_: Request) -> Response:
        # FastMCP stateless mode returns 405 for DELETE, but Claude Code's MCP
        # client treats 405 as fatal. Return 200 so it can reconnect cleanly.
        return Response(status_code=200)

    # Mount doesn't forward lifespan to the inner app, so FastMCP's
    # session_manager.run() — which sets up the anyio task group the
    # streamable-http handler depends on — never fires when mounted. Wire
    # the run() context into the outer app's lifespan ourselves; without
    # this every request 500s with "Task group is not initialized".
    @asynccontextmanager
    async def lifespan(_app: Starlette):
        async with mcp.session_manager.run():
            yield

    # JWT verifier shared across all inbound requests. Construction
    # touches no network — PyJWKClient defers JWKS fetch until first
    # verify — so it's safe at import time.
    verifier = default_verifier()

    return Starlette(
        routes=[
            Route("/healthz", healthz),
            Route("/", delete_session, methods=["DELETE"]),
            Mount("/", app=mcp.streamable_http_app()),
        ],
        middleware=[
            # CallerJWTMiddleware runs first so the resolved Caller is
            # bound to the ContextVar before downstream handlers (and
            # CallerPodIPMiddleware) see the request.
            Middleware(CallerJWTMiddleware, verifier=verifier),
            Middleware(CallerPodIPMiddleware),
        ],
        lifespan=lifespan,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    import uvicorn

    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(build_app(), host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
