"""Browser inspection wrapper used by the MCP tool and CLI.

Browser work runs in the leased test slot's `slot-playwright` pod. mcp-glimmung
talks to it over the Playwright WebSocket protocol through the sibling Node
helper. The MCP host does not run Chromium itself; without an active
test-slot lease there is no browser to drive.

Output discipline: the screenshot PNG is never round-tripped through the
agent's context as base64. The Node helper writes it to a pod-local
tempfile whose lifecycle is owned by the Python wrapper. The wrapper
uploads the bytes to glimmung's `POST /v1/inspections` along with the
full structured report; the MCP tool then returns a compact summary that
references the durable artifact URLs. See glimmung#143 for the design
record.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any


SCRIPT_PATH = Path(__file__).with_name("browser_inspector.mjs")


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(limit - 3, 0)].rstrip() + "..."


def inspect_url(
    *,
    url: str,
    playwright_ws_endpoint: str,
    viewport: dict[str, int] | None = None,
    wait_ms: int = 2000,
    timeout_ms: int = 30000,
    full_page: bool = True,
    capture_accessibility: bool = False,
    capture_console: bool = True,
    capture_network: bool = True,
    max_elements: int = 80,
    body_text_limit: int = 4000,
    cookies: list[dict[str, Any]] | None = None,
    extra_http_headers: dict[str, str] | None = None,
    local_storage: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Inspect a URL via the slot-playwright WebSocket endpoint and return
    the report dict plus a path to the captured PNG.

    `playwright_ws_endpoint` is the slot's `slot-playwright` Service URL, e.g.
    `ws://slot-playwright.<slot-name>.svc.cluster.local:3000`. Callers from the
    MCP tool surface obtain it from the active test-slot lease.

    Auth-injection parameters (`cookies`, `extra_http_headers`,
    `local_storage`) seed the Playwright `BrowserContext` before `page.goto`
    so the navigation runs authenticated. The slot-playwright pod itself
    holds no credentials — every auth identity has to come from the
    caller (the session pod). Typical tank-operator pattern is documented
    in the MCP tool's docstring.

    Lifecycle ownership: this function creates a pod-local tempfile for
    the screenshot, passes its path to the Node helper, reads the report
    JSON off the helper's stdout, and returns both to the caller. The
    caller (tools.py) owns uploading the bytes to glimmung and unlinking
    the tempfile in its `finally` block.
    """
    if not playwright_ws_endpoint:
        raise ValueError("playwright_ws_endpoint is required")
    width = int((viewport or {}).get("width", 1440))
    height = int((viewport or {}).get("height", 900))

    # Tempfile lives in the mcp-glimmung pod's /tmp. We do not delete on
    # creation; tools.py is responsible for unlinking in its finally so a
    # failed subprocess run still cleans up.
    fd, screenshot_path = tempfile.mkstemp(suffix=".png", prefix="inspection-")
    os.close(fd)

    payload: dict[str, Any] = {
        "url": url,
        "playwrightWsEndpoint": playwright_ws_endpoint,
        "viewport": {"width": width, "height": height},
        "waitMs": wait_ms,
        "timeoutMs": timeout_ms,
        "fullPage": full_page,
        "captureAccessibility": capture_accessibility,
        "captureConsole": capture_console,
        "captureNetwork": capture_network,
        "maxElements": max_elements,
        "bodyTextLimit": body_text_limit,
        "screenshotPath": screenshot_path,
    }
    if cookies is not None:
        if not isinstance(cookies, list) or any(not isinstance(c, dict) for c in cookies):
            raise ValueError("cookies must be a list of dicts")
        payload["cookies"] = cookies
    if extra_http_headers is not None:
        if not isinstance(extra_http_headers, dict) or any(
            not isinstance(k, str) or not isinstance(v, str)
            for k, v in extra_http_headers.items()
        ):
            raise ValueError("extra_http_headers must be a dict of str -> str")
        payload["extraHttpHeaders"] = extra_http_headers
    if local_storage is not None:
        if not isinstance(local_storage, dict) or any(
            not isinstance(origin, str)
            or not isinstance(items, dict)
            or any(not isinstance(k, str) or not isinstance(v, str) for k, v in items.items())
            for origin, items in local_storage.items()
        ):
            raise ValueError(
                "local_storage must be a dict of origin -> dict of str -> str"
            )
        payload["localStorage"] = local_storage
    env = os.environ.copy()
    if "NODE_PATH" not in env:
        node_modules = Path.cwd() / "node_modules"
        if node_modules.exists():
            env["NODE_PATH"] = str(node_modules)
    proc = subprocess.run(
        ["node", str(SCRIPT_PATH)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    if proc.returncode != 0:
        # Caller owns unlinking the tempfile; surface the helper error.
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"browser inspection failed: {detail}")
    report = json.loads(proc.stdout)
    # MJS writes `screenshot_path` (the path it received). Surface
    # it back to the caller so the upload + unlink flow is symmetric.
    report["screenshot_path"] = screenshot_path
    return report


def summary_view(
    report: dict[str, Any],
    *,
    inspection_id: str,
    report_url: str,
    screenshot_url: str,
    scope: str,
    scope_ref: str,
    body_text_limit: int = 4000,
    max_elements: int = 80,
    max_console_messages: int = 50,
    max_network_events: int = 50,
) -> dict[str, Any]:
    """Render the compact summary view of an inspection that becomes the
    MCP tool's return value.

    The summary is intentionally small and bounded: the full structured
    record lives in `report.json` under the artifact URL. This view
    carries the references plus a few key fields a model can act on
    without fetching the artifact.
    """
    body_text_preview = _truncate(str(report.get("body_text") or ""), body_text_limit)
    elements = report.get("elements") or []
    if not isinstance(elements, list):
        elements = []
    elements_preview = list(elements[:max_elements])
    console_messages = report.get("console") or []
    page_errors = report.get("page_errors") or []
    failed_requests = report.get("failed_requests") or []
    http_errors = report.get("http_errors") or []

    def _count(items: Any) -> int:
        return len(items) if isinstance(items, list) else 0

    console_error_count = 0
    if isinstance(console_messages, list):
        console_error_count = sum(1 for m in console_messages if isinstance(m, dict) and m.get("type") == "error")

    return {
        "inspection_id": inspection_id,
        "report_url": report_url,
        "screenshot_url": screenshot_url,
        "scope": scope,
        "scope_ref": scope_ref,
        "final_url": report.get("final_url") or "",
        "status": report.get("status"),
        "title": report.get("title") or "",
        "body_text_preview": body_text_preview,
        "elements_preview": elements_preview,
        "console_messages_preview": _truncate_list(console_messages, max_console_messages),
        "console_error_count": console_error_count,
        "page_error_count": _count(page_errors),
        "page_errors_preview": _truncate_list(page_errors, max_console_messages),
        "http_error_count": _count(http_errors),
        "http_errors_preview": _truncate_list(http_errors, max_network_events),
        "failed_request_count": _count(failed_requests),
        "failed_requests_preview": _truncate_list(failed_requests, max_network_events),
        "inspected_at": report.get("inspected_at") or "",
    }


def _truncate_list(value: Any, limit: int) -> list[Any]:
    if not isinstance(value, list):
        return []
    if limit <= 0:
        return []
    return list(value[:limit])


def fresh_inspection_request_id() -> str:
    """Mint the X-Inspection-Request-Id header value used for idempotent
    retries against POST /v1/inspections."""
    return uuid.uuid4().hex


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Inspect a URL with the leased slot's Playwright and emit JSON.",
    )
    parser.add_argument("url")
    parser.add_argument(
        "--playwright-ws-endpoint",
        required=True,
        help="ws:// URL of the slot's slot-playwright Service",
    )
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--wait-ms", type=int, default=2000)
    parser.add_argument("--timeout-ms", type=int, default=30000)
    parser.add_argument("--no-full-page", action="store_true")
    parser.add_argument("--accessibility", action="store_true")
    parser.add_argument("--max-elements", type=int, default=80)
    parser.add_argument("--body-text-limit", type=int, default=4000)
    args = parser.parse_args()

    result = inspect_url(
        url=args.url,
        playwright_ws_endpoint=args.playwright_ws_endpoint,
        viewport={"width": args.width, "height": args.height},
        wait_ms=args.wait_ms,
        timeout_ms=args.timeout_ms,
        full_page=not args.no_full_page,
        capture_accessibility=args.accessibility,
        max_elements=args.max_elements,
        body_text_limit=args.body_text_limit,
    )
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    # CLI mode leaves the tempfile on disk — operators invoking the CLI
    # directly typically want to keep the screenshot. Path is in the JSON.


if __name__ == "__main__":
    main()
