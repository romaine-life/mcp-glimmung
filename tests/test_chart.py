"""Regression tests for the Helm chart's auth.romaine.life token contract."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_deployment_projects_auth_romaine_life_audience_token() -> None:
    deployment = (ROOT / "chart/templates/deployment.yaml").read_text()

    # The Stage B switchover replaced the tank-operator-audience SA
    # token (used directly as the outbound bearer) with an
    # auth.romaine.life-audience SA token that gets exchanged for a
    # role=service JWT. Both glimmung and tank-operator verify the JWT
    # against auth.romaine.life's JWKS, so the projected token's
    # audience must match the IdP's issuer (which is also the audience
    # the IdP's verifyK8sSAToken pins).
    assert "AUTH_ROMAINE_LIFE_SA_TOKEN_PATH" in deployment
    assert "value: /var/run/secrets/auth.romaine.life/token" in deployment
    assert "name: auth-romaine-sa-token" in deployment
    assert "audience: https://auth.romaine.life" in deployment
    assert "expirationSeconds: 3600" in deployment

    # The tank-operator-audience token volume + env var are retired.
    # mcp-glimmung now obtains its tank-operator credential via the
    # exchanged role=service JWT, same as for glimmung.
    assert "TANK_OPERATOR_SA_TOKEN_PATH" not in deployment
    assert "audience: tank-operator" not in deployment
