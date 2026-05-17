"""Browser inspection wrapper used by the MCP tool and CLI.

Browser work runs in the leased test slot's `slot-playwright` pod. mcp-glimmung
talks to it over the Playwright WebSocket protocol through the sibling Node
helper. The MCP host does not run Chromium itself; without an active
test-slot lease there is no browser to drive.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
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
    screenshot: bool = True,
    full_page: bool = True,
    capture_accessibility: bool = False,
    capture_console: bool = True,
    capture_network: bool = True,
    max_elements: int = 80,
    body_text_limit: int = 4000,
) -> dict[str, Any]:
    """Inspect a URL via the slot-playwright WebSocket endpoint and return JSON.

    `playwright_ws_endpoint` is the slot's `slot-playwright` Service URL, e.g.
    `ws://slot-playwright.<slot-name>.svc.cluster.local:3000`. Callers from the
    MCP tool surface obtain it from the active test-slot lease.
    """
    if not playwright_ws_endpoint:
        raise ValueError("playwright_ws_endpoint is required")
    width = int((viewport or {}).get("width", 1440))
    height = int((viewport or {}).get("height", 900))
    payload = {
        "url": url,
        "playwrightWsEndpoint": playwright_ws_endpoint,
        "viewport": {"width": width, "height": height},
        "waitMs": wait_ms,
        "timeoutMs": timeout_ms,
        "screenshot": screenshot,
        "fullPage": full_page,
        "captureAccessibility": capture_accessibility,
        "captureConsole": capture_console,
        "captureNetwork": capture_network,
        "maxElements": max_elements,
        "bodyTextLimit": body_text_limit,
    }
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
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"browser inspection failed: {detail}")
    return json.loads(proc.stdout)


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
    parser.add_argument("--no-screenshot", action="store_true")
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
        screenshot=not args.no_screenshot,
        full_page=not args.no_full_page,
        capture_accessibility=args.accessibility,
        max_elements=args.max_elements,
        body_text_limit=args.body_text_limit,
    )
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
