"""HTTP entrypoint — streamable-http transport, no incoming auth.

Auth is handled by kube-rbac-proxy in front of this process: clients present
a K8s SA token, the proxy validates it via TokenReview + SubjectAccessReview,
and only authorized requests reach this server. Binding loopback so direct
pod-IP:8080 access bypasses nothing — only the proxy can talk to us.

Outgoing glimmung auth is the pod's own projected SA token, presented as a
bearer to glimmung. Glimmung validates via TokenReview against the cluster
API and checks `mcp-glimmung/mcp-glimmung` against its K8S_SA_ALLOWLIST.
"""

import logging
import os
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Mount, Route

from .caller import CALLER_POD_IP, extract_source_pod_ip
from .glimmung_client import GlimmungClient
from .tank_client import TankClient
from .tools import register_tools


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

    return Starlette(
        routes=[
            Route("/healthz", healthz),
            Route("/", delete_session, methods=["DELETE"]),
            Mount("/", app=mcp.streamable_http_app()),
        ],
        middleware=[
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
