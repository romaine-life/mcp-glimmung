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
    # Auth-injection fields are absent unless explicitly passed.
    assert "cookies" not in captured["payload"]
    assert "extraHttpHeaders" not in captured["payload"]
    assert "localStorage" not in captured["payload"]


def test_inspect_url_forwards_auth_injection_to_node_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The whole point of the inject params: drive the slot's Playwright
    # while authenticated as the calling service principal. The Python
    # wrapper's only job is to forward the payload as-is — Playwright is
    # what consumes the shape — so this test pins the contract at the
    # subprocess boundary.
    captured: dict[str, Any] = {}

    class FakeCompleted:
        returncode = 0
        stdout = json.dumps({"schema_version": 2, "url": "https://example.test/"})
        stderr = ""

    def fake_run(cmd: list[str], **kwargs: Any) -> FakeCompleted:
        captured["payload"] = json.loads(kwargs["input"])
        return FakeCompleted()

    monkeypatch.setattr(browser_inspector.subprocess, "run", fake_run)

    cookies = [
        {
            "name": "auth_token",
            "value": "fake.jwt.value",
            "url": "https://tank-operator-slot-1.tank.dev.romaine.life",
            "httpOnly": True,
            "secure": True,
            "sameSite": "Lax",
        }
    ]
    extra_http_headers = {"X-Test-Header": "ok"}
    local_storage = {
        "https://tank-operator-slot-1.tank.dev.romaine.life": {
            "tank-operator-jwt": "fake.jwt.value",
        }
    }

    inspect_url(
        url="https://tank-operator-slot-1.tank.dev.romaine.life",
        playwright_ws_endpoint="ws://slot-playwright.tank-operator-slot-1.svc.cluster.local:3000",
        cookies=cookies,
        extra_http_headers=extra_http_headers,
        local_storage=local_storage,
        wait_ms=0,
        screenshot=False,
    )

    assert captured["payload"]["cookies"] == cookies
    assert captured["payload"]["extraHttpHeaders"] == extra_http_headers
    assert captured["payload"]["localStorage"] == local_storage


@pytest.mark.parametrize(
    "kwargs",
    [
        # Must be a list of dicts.
        {"cookies": "not-a-list"},
        {"cookies": [{"name": "x", "value": "y"}, "not-a-dict"]},
        # Must be a flat str -> str dict.
        {"extra_http_headers": "not-a-dict"},
        {"extra_http_headers": {"good": 1}},
        # Must be origin -> dict of str -> str.
        {"local_storage": "not-a-dict"},
        {"local_storage": {"https://x": "not-a-dict"}},
        {"local_storage": {"https://x": {"good": 1}}},
    ],
)
def test_inspect_url_rejects_malformed_auth_injection(
    monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, Any]
) -> None:
    # Catch obvious shape errors at the Python boundary so the failure
    # mode is a clear ValueError instead of an opaque Node stderr that
    # would only surface once the subprocess runs.
    def _should_not_run(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("subprocess.run should not be invoked on validation error")

    monkeypatch.setattr(browser_inspector.subprocess, "run", _should_not_run)

    with pytest.raises(ValueError):
        inspect_url(
            url="https://example.test/",
            playwright_ws_endpoint="ws://x",
            wait_ms=0,
            screenshot=False,
            **kwargs,
        )
