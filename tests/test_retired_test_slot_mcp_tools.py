"""Migration guard: the agent-facing test-slot *provisioning* MCP tools stay retired.

Test-slot provisioning is now deterministic and server-side: Tank's
``POST /api/sessions/{id}/test-workflow/start`` button/endpoint validates
readiness and drives Glimmung's ``/v1/test-slots/checkout`` +
``/v1/test-slots/deploy-image`` HTTP APIs from inside Tank's backend. The
agent-facing ``checkout_test_slot`` / ``deploy_image_to_test_slot`` MCP tool
wrappers were removed so the model can no longer provision slots by hand.

The underlying HTTP endpoints and the kept session-facing tools
(``return_test_slot``, ``inspect_browser_url``, ``repair_test_slot``,
``extend_test_slot_lease``, ``set_test_environment_count``) are intentionally
NOT covered here — only the retired *provisioning* wrappers.

This guard fails if either retired tool name reappears as a registered tool or
in live ``src/`` code, so the deterministic cutover cannot be silently undone.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))

# Assembled from fragments so this guard file never matches its own scan.
RETIRED_PROVISIONING_TOOLS = (
    "checkout_test_" + "slot",
    "deploy_image_to_test_" + "slot",
)


class _CollectingMCP:
    """Minimal FastMCP stand-in that records every @mcp.tool() registration."""

    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self) -> Any:
        def decorate(fn: Any) -> Any:
            self.tools[fn.__name__] = fn
            return fn

        return decorate


def test_retired_provisioning_tools_are_not_registered() -> None:
    from mcp_glimmung.tools import register_tools

    mcp = _CollectingMCP()
    register_tools(mcp, None)  # type: ignore[arg-type]

    offenders = [name for name in RETIRED_PROVISIONING_TOOLS if name in mcp.tools]
    assert not offenders, (
        "retired agent-facing test-slot provisioning tools are registered again: "
        + ", ".join(offenders)
        + " — provisioning is deterministic/server-side via Tank's Test "
        "button/endpoint; do not re-expose these MCP wrappers"
    )


def test_retired_provisioning_tool_names_absent_from_live_src() -> None:
    failures: list[str] = []
    for path in SRC_ROOT.rglob("*.py"):
        try:
            content = path.read_text()
        except UnicodeDecodeError:
            continue
        for token in RETIRED_PROVISIONING_TOOLS:
            if token in content:
                rel = path.relative_to(REPO_ROOT).as_posix()
                failures.append(f"{rel}: {token}")
    assert not failures, (
        "retired test-slot provisioning tool names reappear in live src:\n"
        + "\n".join(failures)
    )
