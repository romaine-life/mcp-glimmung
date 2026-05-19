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
    cookies: list[dict[str, Any]] | None = None,
    extra_http_headers: dict[str, str] | None = None,
    local_storage: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Inspect a URL via the slot-playwright WebSocket endpoint and return JSON.

    `playwright_ws_endpoint` is the slot's `slot-playwright` Service URL, e.g.
    `ws://slot-playwright.<slot-name>.svc.cluster.local:3000`. Callers from the
    MCP tool surface obtain it from the active test-slot lease.

    Auth-injection parameters (`cookies`, `extra_http_headers`,
    `local_storage`) seed the Playwright `BrowserContext` before `page.goto`
    so the navigation runs authenticated. The slot-playwright pod itself
    holds no credentials — every auth identity has to come from the
    caller (the session pod). Typical tank-operator pattern:

      1. Caller exchanges its projected SA token at
         `auth.romaine.life/api/auth/exchange/k8s` for a `role=service`
         JWT.
      2. Caller POSTs that to the target tank-operator
         `/api/auth/exchange` to mint an `auth_token` session JWT.
      3. Caller passes the resulting cookie to this function:
            cookies=[{
                "name": "auth_token",
                "value": "<minted jwt>",
                "url": "https://tank-operator-slot-1.tank.dev.romaine.life",
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            }]
         (Playwright accepts `url=…` as a shortcut for matching
         `domain`+`path`+`secure`.)

    All three injection params are forwarded as-is to Playwright's
    `context.addCookies`, `context.setExtraHTTPHeaders`, and an
    `addInitScript` that seeds `window.localStorage` per origin. Detailed
    schema validation (`sameSite` enum, `url` vs `domain`+`path`
    exclusivity, etc.) is left to Playwright — its error text is more
    precise than anything this wrapper can pre-validate, and it bubbles
    up through the subprocess stderr.
    """
    if not playwright_ws_endpoint:
        raise ValueError("playwright_ws_endpoint is required")
    width = int((viewport or {}).get("width", 1440))
    height = int((viewport or {}).get("height", 900))
    payload: dict[str, Any] = {
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
