import json
import os
from pathlib import Path
from typing import Any

import pytest

from mcp_glimmung import browser_inspector
from mcp_glimmung.browser_inspector import (
    _truncate,
    fresh_inspection_request_id,
    inspect_url,
    summary_view,
)


def test_truncate_caps_long_text() -> None:
    assert _truncate("abcdef", 4) == "a..."
    assert _truncate("abc", 4) == "abc"


def test_inspect_url_rejects_missing_endpoint() -> None:
    with pytest.raises(ValueError, match="playwright_ws_endpoint is required"):
        inspect_url(url="https://example.test/", playwright_ws_endpoint="")


def _fake_run_writing_png(captured: dict[str, Any]) -> Any:
    """Return a fake subprocess.run that mimics MJS: writes PNG bytes to
    the tempfile Python provided and prints the report JSON on stdout
    with screenshot_path set to that same path."""

    def _run(cmd: list[str], **kwargs: Any) -> Any:
        payload = json.loads(kwargs["input"])
        captured["payload"] = payload
        captured["cmd"] = cmd
        path = payload["screenshotPath"]
        # Mimic Playwright's `path=` write of a PNG.
        Path(path).write_bytes(b"PNG-BYTES")
        report = {
            "schema_version": 2,
            "url": payload["url"],
            "final_url": payload["url"],
            "status": 200,
            "title": "fake",
            "body_text": "hello",
            "elements": [],
            "console": [],
            "page_errors": [],
            "failed_requests": [],
            "http_errors": [],
            "accessibility": None,
            "screenshot_path": path,
            "screenshot_size_bytes": 9,
            "canvas": {"readable": True, "nonblank_pixels": 0, "sampled_pixels": 0},
            "inspected_at": "2026-05-28T00:00:00.000Z",
        }

        class FakeCompleted:
            returncode = 0
            stdout = json.dumps(report)
            stderr = ""

        return FakeCompleted()

    return _run


def test_inspect_url_forwards_payload_and_returns_screenshot_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(browser_inspector.subprocess, "run", _fake_run_writing_png(captured))

    result = inspect_url(
        url="https://example.test/",
        playwright_ws_endpoint="ws://slot-playwright.tank-operator-slot-1.svc.cluster.local:3000",
        wait_ms=0,
    )

    payload = captured["payload"]
    assert payload["url"] == "https://example.test/"
    assert payload["screenshotPath"].endswith(".png")
    assert "screenshot" not in payload, "deprecated `screenshot` flag should not leak to MJS payload"
    assert "screenshot_base64" not in result, "screenshot_base64 must not surface"
    assert result["screenshot_path"] == payload["screenshotPath"]
    # PNG written to the tempfile by the fake MJS.
    assert Path(result["screenshot_path"]).read_bytes() == b"PNG-BYTES"
    # Test owns cleanup; the real wrapper leaves it for tools.py.
    os.unlink(result["screenshot_path"])


def test_inspect_url_forwards_auth_injection_to_node_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(browser_inspector.subprocess, "run", _fake_run_writing_png(captured))

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

    result = inspect_url(
        url="https://tank-operator-slot-1.tank.dev.romaine.life",
        playwright_ws_endpoint="ws://slot-playwright.tank-operator-slot-1.svc.cluster.local:3000",
        cookies=cookies,
        extra_http_headers=extra_http_headers,
        local_storage=local_storage,
        wait_ms=0,
    )

    payload = captured["payload"]
    assert payload["cookies"] == cookies
    assert payload["extraHttpHeaders"] == extra_http_headers
    assert payload["localStorage"] == local_storage
    os.unlink(result["screenshot_path"])


@pytest.mark.parametrize(
    "kwargs",
    [
        {"cookies": "not-a-list"},
        {"cookies": [{"name": "x", "value": "y"}, "not-a-dict"]},
        {"extra_http_headers": "not-a-dict"},
        {"extra_http_headers": {"good": 1}},
        {"local_storage": "not-a-dict"},
        {"local_storage": {"https://x": "not-a-dict"}},
        {"local_storage": {"https://x": {"good": 1}}},
    ],
)
def test_inspect_url_rejects_malformed_auth_injection(
    monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, Any]
) -> None:
    def _should_not_run(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("subprocess.run should not be invoked on validation error")

    monkeypatch.setattr(browser_inspector.subprocess, "run", _should_not_run)

    with pytest.raises(ValueError):
        inspect_url(
            url="https://example.test/",
            playwright_ws_endpoint="ws://x",
            wait_ms=0,
            **kwargs,
        )


def test_summary_view_bounds_console_and_network() -> None:
    report = {
        "final_url": "https://example.test/",
        "status": 200,
        "title": "T",
        "body_text": "x" * 500,
        "elements": [{"selector": str(i)} for i in range(200)],
        "console": [{"type": "log", "text": str(i)} for i in range(200)],
        "page_errors": [str(i) for i in range(200)],
        "failed_requests": [{"url": str(i)} for i in range(200)],
        "http_errors": [{"url": str(i), "status": 500} for i in range(200)],
        "inspected_at": "2026-05-28T00:00:00.000Z",
    }
    summary = summary_view(
        report,
        inspection_id="i1",
        report_url="/v1/artifacts/inspections/L/i1/report.json",
        screenshot_url="/v1/artifacts/inspections/L/i1/screenshot.png",
        scope="lease",
        scope_ref="L",
        body_text_limit=100,
        max_elements=3,
        max_console_messages=2,
        max_network_events=4,
    )
    assert "screenshot_base64" not in summary
    assert summary["inspection_id"] == "i1"
    assert summary["scope"] == "lease"
    assert summary["scope_ref"] == "L"
    assert summary["report_url"].startswith("/v1/artifacts/inspections/")
    assert summary["screenshot_url"].startswith("/v1/artifacts/inspections/")
    assert len(summary["body_text_preview"]) <= 100
    assert len(summary["elements_preview"]) == 3
    assert len(summary["console_messages_preview"]) == 2
    assert len(summary["http_errors_preview"]) == 4
    assert len(summary["failed_requests_preview"]) == 4
    assert summary["console_error_count"] == 0  # none of the fake logs are 'error'
    assert summary["http_error_count"] == 200


def test_fresh_inspection_request_id_is_unique() -> None:
    ids = {fresh_inspection_request_id() for _ in range(16)}
    assert len(ids) == 16
    for s in ids:
        assert len(s) == 32  # uuid4 hex
