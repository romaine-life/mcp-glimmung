"""Regression tests for the Helm chart's deployment contract.

mcp-glimmung no longer mints its own outbound JWT — every request
forwards the inbound caller's auth.romaine.life JWT directly, so the
projected SA token mount is retired. These tests guard against
reintroducing it.
"""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_deployment_does_not_mount_auth_romaine_sa_token() -> None:
    deployment = (ROOT / "chart/templates/deployment.yaml").read_text()

    # The pod-stable consumer model (mcp-glimmung exchanges its own SA
    # token at /api/auth/exchange/k8s, mints a synthetic identity, and
    # uses it for outbound calls) was retired in favor of forwarding the
    # inbound caller's JWT directly. No SA token mount, no env var, no
    # outbound exchange.
    assert "AUTH_ROMAINE_LIFE_SA_TOKEN_PATH" not in deployment
    assert "auth-romaine-sa-token" not in deployment
    assert "audience: https://auth.romaine.life" not in deployment

    # The earlier (pre-#37) tank-operator-audience token is also gone.
    assert "TANK_OPERATOR_SA_TOKEN_PATH" not in deployment
    assert "audience: tank-operator" not in deployment
