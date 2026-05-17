import json
from pathlib import Path
from typing import Any

import pytest

from mcp_glimmung import browser_inspector
from mcp_glimmung.browser_inspector import _truncate, inspect_url


def test_truncate_caps_long_text() -> None:
    assert _truncate("abcdef", 4) == "a..."
    assert _truncate("abc", 4) == "abc"


def test_inspect_url_rejects_missing_endpoint() -> None:
    with pytest.raises(ValueError, match="playwright_ws_endpoint is required"):
        inspect_url(url="https://example.test/", playwright_ws_endpoint="")


def test_inspect_url_forwards_payload_to_node_helper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, Any] = {}

    fake_response = {
        "schema_version": 2,
        "url": "https://example.test/",
        "screenshot_base64": "QUJD",
    }

    class FakeCompleted:
        returncode = 0
        stdout = json.dumps(fake_response)
        stderr = ""

    def fake_run(cmd: list[str], **kwargs: Any) -> FakeCompleted:
        captured["cmd"] = cmd
        captured["payload"] = json.loads(kwargs["input"])
        return FakeCompleted()

    monkeypatch.setattr(browser_inspector.subprocess, "run", fake_run)

    result = inspect_url(
        url="https://example.test/",
        playwright_ws_endpoint="ws://slot-playwright.tank-operator-slot-1.svc.cluster.local:3000",
        wait_ms=0,
        screenshot=True,
    )

    assert result == fake_response
    assert captured["payload"]["url"] == "https://example.test/"
    assert (
        captured["payload"]["playwrightWsEndpoint"]
        == "ws://slot-playwright.tank-operator-slot-1.svc.cluster.local:3000"
    )
    assert captured["payload"]["screenshot"] is True
    assert "artifactDir" not in captured["payload"]
