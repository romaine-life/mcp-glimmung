"""Regression tests for the Helm chart's Tank token contract."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_deployment_projects_tank_operator_audience_token() -> None:
    deployment = (ROOT / "chart/templates/deployment.yaml").read_text()

    assert "TANK_OPERATOR_SA_TOKEN_PATH" in deployment
    assert "value: /var/run/secrets/tank-operator/token" in deployment
    assert "name: tank-operator-sa-token" in deployment
    assert "audience: tank-operator" in deployment
    assert "expirationSeconds: 3600" in deployment
