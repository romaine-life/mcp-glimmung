"""mcp-glimmung tools — typed wrappers over glimmung's HTTP API.

Read surface plus session-safe mutations. Lease and webhook endpoints stay
unexposed — those are runner / orchestrator concerns, not session concerns.
"""

import logging
import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

from .browser_inspector import inspect_url
from .caller import current_caller_pod_ip
from .glimmung_client import GlimmungClient
from .tank_client import TankClient

_CALLER_MISSING_MSG = (
    "could not identify caller from pod IP - make sure you're calling from "
    "inside a tank-operator session pod"
)
log = logging.getLogger(__name__)


def _pod_ip() -> str:
    ip = current_caller_pod_ip()
    if not ip:
        raise ValueError(_CALLER_MISSING_MSG)
    return ip


def _tank_session_id(value: str) -> str:
    """Normalize either a Tank session id or its Kubernetes pod name."""
    return value.removeprefix("session-")


def _lease_label(lease: dict[str, Any]) -> str:
    number = lease.get("lease_number")
    if number is not None:
        return f"#{number}"
    metadata = lease.get("metadata") if isinstance(lease.get("metadata"), dict) else {}
    slot_name = metadata.get("native_slot_name")
    if isinstance(slot_name, str) and slot_name:
        return slot_name
    issue_number = metadata.get("issue_number") or metadata.get("issueNumber")
    if issue_number:
        return f"issue #{issue_number}"
    return "lease"


def _sanitize_state_for_sessions(state: dict[str, Any]) -> dict[str, Any]:
    leases = [
        lease
        for lease in (
            list(state.get("pending_leases") or [])
            + list(state.get("active_leases") or [])
        )
        if isinstance(lease, dict)
    ]
    labels_by_id = {
        lease["id"]: _lease_label(lease)
        for lease in leases
        if isinstance(lease.get("id"), str)
    }

    sanitized = dict(state)
    for key in ("pending_leases", "active_leases"):
        sanitized[key] = [
            {
                **{k: v for k, v in lease.items() if k != "id"},
                "lease": _lease_label(lease),
            }
            for lease in (state.get(key) or [])
            if isinstance(lease, dict)
        ]
    sanitized["hosts"] = [
        {
            **{k: v for k, v in host.items() if k != "current_lease_id"},
            "current_lease": labels_by_id.get(host.get("current_lease_id"), "active lease")
            if host.get("current_lease_id")
            else None,
        }
        for host in (state.get("hosts") or [])
        if isinstance(host, dict)
    ]
    return sanitized


def _hide_lease_id(result: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(result)
    if "lease_id" in sanitized:
        sanitized.pop("lease_id", None)
        sanitized.setdefault("lease", _result_lease_label(result))
    return sanitized


def _result_lease_label(result: dict[str, Any]) -> str:
    number = result.get("lease_number")
    if number is not None:
        return f"#{number}"
    slot_name = result.get("slot_name")
    if isinstance(slot_name, str) and slot_name:
        return slot_name
    return "claimed"


def _run_display(value: int | str) -> str:
    display = str(value).strip()
    if not display:
        raise ValueError("run_number required")
    return display


def _as_positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str):
        try:
            parsed = int(value.strip())
        except ValueError:
            return None
        if parsed > 0:
            return parsed
    return None


def _lease_metadata(lease: dict[str, Any]) -> dict[str, Any]:
    metadata = lease.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _lease_slot_name(lease: dict[str, Any]) -> str | None:
    metadata = _lease_metadata(lease)
    value = (
        lease.get("slot_name")
        or lease.get("native_slot_name")
        or metadata.get("slot_name")
        or metadata.get("native_slot_name")
    )
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _lease_slot_index(lease: dict[str, Any]) -> int | None:
    metadata = _lease_metadata(lease)
    for value in (
        lease.get("slot_index"),
        lease.get("native_slot_index"),
        metadata.get("slot_index"),
        metadata.get("native_slot_index"),
    ):
        parsed = _as_positive_int(value)
        if parsed is not None:
            return parsed
    return None


def _hot_swap_selector_problem(
    project: str,
    slot_name: str | None,
    slot_index: int | None,
) -> dict[str, Any] | None:
    has_name = isinstance(slot_name, str) and bool(slot_name.strip())
    has_index = slot_index is not None
    if has_name == has_index:
        return {
            "state": "slot_selector_invalid",
            "project": project,
            "slot_name": slot_name,
            "slot_index": slot_index,
            "detail": "pass exactly one of slot_name or slot_index",
        }
    return None


def _active_hot_swap_lease_problem(
    client: GlimmungClient,
    project: str,
    slot_name: str | None,
    slot_index: int | None,
) -> dict[str, Any] | None:
    selector_problem = _hot_swap_selector_problem(project, slot_name, slot_index)
    if selector_problem is not None:
        return selector_problem

    state = client.get("/v1/state")
    active = state.get("active_leases") if isinstance(state, dict) else []
    for lease in active or []:
        if not isinstance(lease, dict):
            continue
        if lease.get("project") != project:
            continue
        metadata = _lease_metadata(lease)
        if metadata.get("test_slot_checkout") is False:
            continue
        if slot_name is not None and _lease_slot_name(lease) == slot_name:
            return None
        if slot_index is not None and _lease_slot_index(lease) == slot_index:
            return None

    return {
        "state": "no_active_test_slot_lease",
        "project": project,
        "slot_name": slot_name,
        "slot_index": slot_index,
        "detail": (
            "no active test-slot lease matches this project and slot; "
            "call checkout_test_slot before applying or recording a hot swap"
        ),
    }


def _resolve_slot_playwright_ws(client: GlimmungClient, tank_session_id: str) -> str:
    """Find the active test-slot lease for a Tank session and return its
    slot-playwright Service ws endpoint.

    Errors if the session does not currently hold an active test-slot lease,
    or if the lease's slot does not yet expose a playwright endpoint
    (slot still activating, or the cluster is not running playwright-enabled
    slots). Sessions must `checkout_test_slot` before calling
    `inspect_browser_url`.
    """
    if not tank_session_id:
        raise ValueError("tank_session_id required")
    state = client.get("/v1/state")
    active_leases = state.get("active_leases") or []
    for lease in active_leases:
        if not isinstance(lease, dict):
            continue
        # Glimmung exposes the tank session id at any of three places on
        # the lease envelope, depending on how the lease was minted:
        #   1. lease["metadata"]["tank_session_id"] — older flat shape
        #   2. lease["metadata"]["requester"]["metadata"]["tank_session_id"]
        #   3. lease["requester"]["metadata"]["tank_session_id"]
        # (2) and (3) are what the native-k8s test-slot allocator writes
        # today — (1) was never populated by checkout_test_slot, so the
        # lookup found nothing and every inspect_browser_url call landed
        # on the "no active test-slot lease" error path. Try all three
        # in deterministic order; first non-empty match wins.
        metadata = lease.get("metadata") if isinstance(lease.get("metadata"), dict) else {}
        nested_requester = metadata.get("requester") if isinstance(metadata.get("requester"), dict) else {}
        nested_requester_md = (
            nested_requester.get("metadata")
            if isinstance(nested_requester.get("metadata"), dict)
            else {}
        )
        top_requester = lease.get("requester") if isinstance(lease.get("requester"), dict) else {}
        top_requester_md = (
            top_requester.get("metadata")
            if isinstance(top_requester.get("metadata"), dict)
            else {}
        )
        lease_session_id = (
            metadata.get("tank_session_id")
            or metadata.get("tankSessionId")
            or nested_requester_md.get("tank_session_id")
            or nested_requester_md.get("tankSessionId")
            or top_requester_md.get("tank_session_id")
            or top_requester_md.get("tankSessionId")
        )
        if lease_session_id != tank_session_id:
            continue
        endpoint = (
            lease.get("playwright_ws_endpoint")
            or metadata.get("playwright_ws_endpoint")
        )
        if isinstance(endpoint, str) and endpoint:
            return endpoint
        raise RuntimeError(
            f"test slot for tank session {tank_session_id!r} has no "
            "playwright_ws_endpoint yet; slot may still be activating or "
            "glimmung is not running playwright-enabled slots"
        )
    raise RuntimeError(
        f"no active test-slot lease found for tank session {tank_session_id!r}; "
        "call checkout_test_slot before inspect_browser_url"
    )


def register_tools(
    mcp: FastMCP, client: GlimmungClient, tank_client: TankClient | None = None
) -> None:
    @mcp.tool()
    def get_issue(repo_owner: str, repo_name: str, issue_number: int) -> dict[str, Any]:
        """Deprecated: GitHub Issue lookup is disabled.

        Use `get_issue_by_number(project, issue_number)` instead."""
        raise RuntimeError("GitHub Issue lookup is disabled; use project-native issue lookup")

    @mcp.tool()
    def get_issue_by_number(project: str, issue_number: int) -> dict[str, Any]:
        """Get a Glimmung issue by project and project-scoped issue number."""
        return client.get(f"/v1/issues/by-number/{project}/{issue_number}")

    @mcp.tool()
    def get_issue_graph(repo_owner: str, repo_name: str, issue_number: int) -> dict[str, Any]:
        """Deprecated: GitHub Issue graph lookup is disabled.

        Use `get_issue_graph_by_number(project, issue_number)` instead."""
        raise RuntimeError("GitHub Issue graph lookup is disabled; use project-native issue lookup")

    @mcp.tool()
    def get_issue_graph_by_number(project: str, issue_number: int) -> dict[str, Any]:
        """Get the Glimmung lineage graph for one project-scoped issue."""
        return client.get(f"/v1/issues/by-number/{project}/{issue_number}/graph")

    @mcp.tool()
    def get_run_report(project: str, issue_number: int, run_number: str) -> dict[str, Any]:
        """Get one RunReport by issue-scoped run display number.

        Use this for normal operator work: "run 1.3 for glimmung#141"
        maps to `project="glimmung", issue_number=141, run_number="1.3"`.
        """
        run_number = _run_display(run_number)
        return client.get(
            f"/v1/projects/{project}/issues/{issue_number}/runs/{run_number}/report",
        )

    @mcp.tool()
    def get_native_run_events(
        project: str,
        issue_number: int,
        run_number: str,
        attempt_index: int | None = None,
        job_id: str | None = None,
        limit: int | None = 200,
    ) -> dict[str, Any]:
        """Read hot native k8s_job step/log events for a Glimmung run.

        Use with graph attempt metadata (`phase_kind == "k8s_job"`) to inspect
        the ordered runner event stream. `attempt_index` narrows to one
        PhaseAttempt, `job_id` narrows to one native job, and `limit` caps the
        returned hot rows. Older archived attempts expose `archive_url` in the
        response when hot rows have been pruned or archived.
        """
        params = {
            "attempt_index": attempt_index,
            "job_id": job_id,
            "limit": limit,
        }
        run_number = _run_display(run_number)
        return client.get(
            f"/v1/projects/{project}/issues/{issue_number}/runs/{run_number}/native/events",
            params={k: v for k, v in params.items() if v is not None},
        )

    @mcp.tool()
    def list_issues(
        project: str | None = None,
        state: str | None = "open",
        limit: int | None = 50,
    ) -> list[dict[str, Any]]:
        """List Glimmung issues across projects, optionally filtered.

        Use to discover issue ids and project names before dispatching or
        patching. `project` filters by Glimmung project name, `state` is
        "open", "closed", or "all", and `limit` caps returned rows.
        """
        params = {
            "project": project,
            "state": state,
            "limit": limit,
        }
        return client.get(
            "/v1/issues",
            params={k: v for k, v in params.items() if v is not None},
        )

    @mcp.tool()
    def get_state() -> dict[str, Any]:
        """Get Glimmung control-plane state: hosts, leases, locks, and recent runs.

        Snapshot of hosts, leases, and recent runs. Same shape the
        /v1/events SSE feed pushes; this returns the latest snapshot
        point-in-time."""
        return _sanitize_state_for_sessions(client.get("/v1/state"))

    @mcp.tool()
    def list_leases(project: str | None = None) -> dict[str, Any]:
        """Check lease availability: native test slots, free hosts, active leases, and pending leases.

        Returns three lists:
        - `available_test_slots`: native test slots with state `available`.
        - `available_hosts`: non-drained registered worker hosts with no current lease.
        - `active_leases`: leases currently holding a host or native slot.
        - `pending_leases`: leases queued but not yet assigned capacity.

        Pass `project` to narrow all three lists to a single project.
        Omit it to see the full cross-project picture."""
        state = _sanitize_state_for_sessions(client.get("/v1/state"))

        hosts = state.get("hosts") or []
        test_slots = state.get("test_environments") or []
        active = state.get("active_leases") or []
        pending = state.get("pending_leases") or []

        if project is not None:
            test_slots = [slot for slot in test_slots if slot.get("project") == project]
            hosts = [
                h for h in hosts
                if isinstance(h.get("capabilities"), dict)
                and h["capabilities"].get("project") == project
            ]
            active = [lease for lease in active if lease.get("project") == project]
            pending = [lease for lease in pending if lease.get("project") == project]

        test_slot_names = {
            slot.get("slot_name")
            for slot in test_slots
            if isinstance(slot.get("slot_name"), str)
        }
        available_hosts = [
            h for h in hosts
            if not h.get("current_lease")
            and not h.get("drained")
            and h.get("name") not in test_slot_names
        ]

        return {
            "available_test_slots": [
                slot for slot in test_slots
                if slot.get("state") == "available"
            ],
            "available_hosts": available_hosts,
            "active_leases": active,
            "pending_leases": pending,
        }

    @mcp.tool()
    def list_projects(
        name: str | None = None,
        github_repo: str | None = None,
        limit: int | None = 50,
    ) -> list[dict[str, Any]]:
        """List configured Glimmung projects and their GitHub repository bindings.

        `name`, `github_repo`, and `limit` narrow large project lists.
        """
        params = {
            "name": name,
            "github_repo": github_repo,
            "limit": limit,
        }
        return client.get(
            "/v1/projects",
            params={k: v for k, v in params.items() if v is not None},
        )

    @mcp.tool()
    def register_project(
        name: str,
        github_repo: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create or update a Glimmung project mapped to a GitHub repository.

        Upsert a glimmung Project. Use this when standing up a new
        repository in the control plane before registering workflows or
        native issues. `github_repo` is the canonical "owner/repo" slug;
        `metadata` is an optional free-form bag preserved on the Project."""
        return client.post(
            "/v1/projects",
            json={
                "name": name,
                "github_repo": github_repo,
                "metadata": metadata or {},
            },
        )

    @mcp.tool()
    def get_test_slot_hot_swap_contract(project: str) -> dict[str, Any]:
        """Read a project's native test-slot hot-swap contract.

        Use before fast validation updates. The returned contract describes
        static copy source/target paths and backend build/artifact/restart
        behavior. If the project has no `metadata.test_slot_hot_swap`, the
        response has `enabled: false` and a diagnostic detail."""
        rows = client.get("/v1/projects", params={"name": project, "limit": 10})
        for row in rows:
            if row.get("name") != project and row.get("id") != project:
                continue
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            contract = metadata.get("test_slot_hot_swap") or metadata.get("testSlotHotSwap")
            if isinstance(contract, dict):
                return {
                    "project": row.get("name") or project,
                    "enabled": bool(contract.get("enabled")),
                    "contract": contract,
                }
            return {
                "project": row.get("name") or project,
                "enabled": False,
                "detail": "project has no test_slot_hot_swap metadata",
            }
        return {
            "project": project,
            "enabled": False,
            "detail": "project not found",
        }

    @mcp.tool()
    def record_test_slot_hot_swap(
        project: str,
        status: str,
        operation: str = "hot_swap",
        lease_ref: str | None = None,
        slot_name: str | None = None,
        slot_index: int | None = None,
        summary: str = "",
        diagnostics: dict[str, Any] | None = None,
        timings: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Append hot-swap diagnostics to an active test-slot lease.

        Use after a static or backend hot swap so later operators can inspect
        recent build/copy/restart/health details from the lease metadata. Pass
        `lease_ref`, or identify the active checkout with `slot_name` or
        `slot_index`.

        When a slot selector is used and no active checkout exists, this
        returns `state: no_active_test_slot_lease` instead of surfacing the
        Glimmung API's generic 404."""
        if not lease_ref:
            problem = _active_hot_swap_lease_problem(client, project, slot_name, slot_index)
            if problem is not None:
                return problem
        payload: dict[str, Any] = {
            "project": project,
            "entry": {
                "operation": operation,
                "status": status,
                "summary": summary,
                "diagnostics": diagnostics or {},
                "timings": timings or {},
            },
        }
        if lease_ref:
            payload["lease_ref"] = lease_ref
        if slot_name:
            payload["slot_name"] = slot_name
        if slot_index is not None:
            payload["slot_index"] = slot_index
        return client.post("/v1/test-slots/hot-swap-history", json=payload)

    @mcp.tool()
    def apply_test_slot_hot_swap(
        project: str,
        artifact_kind: str,
        git_ref: str,
        validation_target: str,
        slot_index: int | None = None,
        slot_name: str | None = None,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Build new code at a git ref and place it onto a running test slot.

        End-to-end developer dev loop, sync UX: this one call blocks until the
        dispatched Kubernetes Job completes (clones the repo at git_ref, runs
        any project-owned fidelity classifier, runs the contract's build_command
        in the contract's builder_image, kubectl-streams the artifact into the
        target session pod, sends the configured restart signal). Then returns a
        structured result.

        The previous workflow (per the /test agent skill) was a manual dance
        of `kubectl cp` + `kubectl exec` + `kill -HUP 1`. This tool replaces
        all of that — the dev's only action is the call.

        Args:
            project: Glimmung project name (e.g., "tank-operator").
            artifact_kind: Which contract sub-block applies. v1 supports
                "agent_runner" and "codex_runner"; static and backend continue
                to use the glimmung-agent CLI path until their consumers opt in.
            git_ref: Branch or tag to clone. Pushed beforehand.
            validation_target: What the hot-swap result is meant to prove.
                Use "existing_session" for already-running target pods,
                "new_session" when you will create a fresh session after the
                change, or "full_runtime" for rollout-equivalent evidence.
                Projects with fidelity_classifier enabled reject omitted or
                incompatible targets.
            slot_index or slot_name: Identifies the active test-slot lease.
                Exactly one of the two should be set.
            timeout_seconds: Server-side bound. Clamped to [1, 600]. Default
                120 (covers 30-90s typical build-and-swap + buffer for cold
                image pulls).

        Returns the structured result from POST /v1/test-slots/apply-hot-swap:
            {"lease": "...", "apply": {"outcome": "persisted" | "build_failed"
              | "swap_failed" | "timeout", "job_name": ..., "target_pods":
              [...], "build_logs_tail": ..., "swap_logs_tail": ..., "timings":
              {...}}, "history_entry": {...}}

        Hot-swap history is recorded on every outcome — durable state lives
        in the lease, not in the response. A caller that disconnects mid-
        request can re-query via the lease's metadata to see the result.

        See docs/test-slot-hot-swap.md in nelsong6/glimmung for the workflow
        contract and the contract shape projects need to declare.
        """
        problem = _active_hot_swap_lease_problem(client, project, slot_name, slot_index)
        if problem is not None:
            return problem

        payload: dict[str, Any] = {
            "project": project,
            "artifact_kind": artifact_kind,
            "git_ref": git_ref,
            "validation_target": validation_target,
        }
        if slot_name:
            payload["slot_name"] = slot_name
        if slot_index is not None:
            payload["slot_index"] = slot_index
        if timeout_seconds is not None:
            payload["timeout_seconds"] = timeout_seconds
        return client.post("/v1/test-slots/apply-hot-swap", json=payload)

    @mcp.tool()
    def list_workflows(
        project: str | None = None,
        name: str | None = None,
        trigger_label: str | None = None,
        limit: int | None = 50,
    ) -> list[dict[str, Any]]:
        """List Glimmung workflow definitions across projects, optionally filtered.

        Use to discover workflow names, phase shapes, trigger labels, PR
        settings, budgets, and requirements before patching or registering.
        `project`, `name`, `trigger_label`, and `limit` narrow large lists.
        """
        params = {
            "project": project,
            "name": name,
            "trigger_label": trigger_label,
            "limit": limit,
        }
        return client.get(
            "/v1/workflows",
            params={k: v for k, v in params.items() if v is not None},
        )

    @mcp.tool()
    def create_playbook(
        project: str,
        title: str,
        description: str = "",
        entries: list[dict[str, Any]] | None = None,
        concurrency_limit: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a draft Glimmung Playbook for coordinating multiple issues.

        Storage-only surface for preserving operator intent across a batch
        of related issues. Entries are not enqueued or dispatched by this
        v1 call; future playbook run semantics will consume the stored
        entry specs. Each entry should include `id` and an `issue` object
        with `title`, optional `body`, `labels`, `workflow`, and metadata.
        """
        payload: dict[str, Any] = {
            "project": project,
            "title": title,
            "description": description,
            "entries": entries or [],
            "metadata": metadata or {},
        }
        if concurrency_limit is not None:
            payload["concurrency_limit"] = concurrency_limit
        return client.post("/v1/playbooks", json=payload)

    @mcp.tool()
    def list_playbooks(
        project: str | None = None,
        state: str | None = None,
        limit: int | None = 50,
    ) -> list[dict[str, Any]]:
        """List Glimmung Playbooks, optionally filtered by project or state."""
        params = {
            "project": project,
            "state": state,
            "limit": limit,
        }
        params = {k: v for k, v in params.items() if v is not None}
        return client.get("/v1/playbooks", params=params)

    @mcp.tool()
    def get_playbook(project: str, playbook_ref: str) -> dict[str, Any]:
        """Get one Glimmung Playbook by project and public playbook ref."""
        return client.get(f"/v1/playbooks/{project}/{playbook_ref}")

    @mcp.tool()
    def run_playbook(project: str, playbook_ref: str) -> dict[str, Any]:
        """Start or advance a Glimmung Playbook.

        The server mints and dispatches ready entries up to the playbook's
        concurrency limit, refreshes linked run outcomes, and records created
        issue/run refs. Re-run this after entries complete to advance
        dependency-gated work.
        """
        return client.post(f"/v1/playbooks/{project}/{playbook_ref}/run")

    @mcp.tool()
    def inspect_browser_url(
        url: str,
        tank_session_id: str,
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
        """Inspect a live URL with Chromium and return browser-state JSON.

        Runs inside the active test slot's `slot-playwright` pod, so callers
        must hold a checked-out test slot via `checkout_test_slot` first. The
        slot's Playwright drives the browser; the MCP host does not. Pass the
        Tank session id whose lease should be used.

        Use for validation URLs when a static screenshot is not enough: the
        tool waits for the rendered page, captures final URL/status,
        title/body summary, interesting DOM elements with selectors and
        bounds, console/page errors, failed requests and HTTP >= 400
        responses, optional accessibility tree, an inline `screenshot_base64`
        PNG, and canvas nonblank sampling data.

        Auth-injection parameters drive an *authenticated* browse — the
        slot-playwright pod holds no credentials of its own, so the
        caller is the only source of identity. Three knobs, all forwarded
        directly to Playwright's `BrowserContext` before `page.goto`:

        - `cookies`: list of Playwright cookie dicts, applied via
          `context.addCookies`. The tank-operator-slot pattern:

              cookies=[{
                  "name": "auth_token",
                  "value": "<minted session jwt>",
                  "url": "https://tank-operator-slot-1.tank.dev.romaine.life",
                  "httpOnly": True,
                  "secure": True,
                  "sameSite": "Lax",
              }]

          The session JWT is what the caller gets back from POSTing its
          `auth.romaine.life` service token to the target slot's
          `/api/auth/exchange`.
        - `extra_http_headers`: dict applied via
          `context.setExtraHTTPHeaders`. Useful for `Authorization:
          Bearer …` on slot URLs that hit JSON APIs.
        - `local_storage`: dict of `origin -> {key: value}`. Seeded by
          an `addInitScript` that runs before every page script, so
          SPAs that boot from `localStorage[tank-operator-jwt]` come
          up already signed in.

        Detailed schema validation (sameSite enum, url vs. domain
        exclusivity, etc.) is delegated to Playwright: its error text
        is more precise than anything this wrapper can pre-validate
        and it bubbles up through the subprocess stderr.
        """
        ws_endpoint = _resolve_slot_playwright_ws(
            client, _tank_session_id(tank_session_id)
        )
        return inspect_url(
            url=url,
            playwright_ws_endpoint=ws_endpoint,
            viewport=viewport,
            wait_ms=wait_ms,
            timeout_ms=timeout_ms,
            screenshot=screenshot,
            full_page=full_page,
            capture_accessibility=capture_accessibility,
            capture_console=capture_console,
            capture_network=capture_network,
            max_elements=max_elements,
            body_text_limit=body_text_limit,
            cookies=cookies,
            extra_http_headers=extra_http_headers,
            local_storage=local_storage,
        )

    @mcp.tool()
    def register_workflow(
        project: str,
        name: str,
        phases: list[dict[str, Any]],
        pr: dict[str, Any] | None = None,
        budget: dict[str, Any] | None = None,
        trigger_label: str | None = None,
        default_requirements: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create or replace a Glimmung workflow registration, including phases, PR policy, budget, and triggers.

        Upsert a Workflow (create or replace). Use this for the
        structural fields `patch_workflow` won't touch: phase shape,
        declared inputs/outputs, recycle policy, and default requirements.
        Trigger labels are legacy metadata; omit unless you are preserving an
        older workflow record for display/filtering.
        Idempotent — re-registering the same shape is a no-op replace, so
        consumer-migration scripts can run repeatedly without piling up state.
        The server preserves `createdAt` on
        replace and validates cross-phase input refs at registration
        time, so a typo in `${{ phases.NAME.outputs.KEY }}` surfaces
        before it can corrupt a run.

        `phases` is a list of PhaseSpec dicts; `gha_dispatch` phases
        declare `workflow_filename`, while `k8s_job` phases declare
        `jobs` with app-owned `steps`. Optional fields: `kind` (default
        "gha_dispatch"), `workflow_ref`, `inputs`, `outputs`,
        `requirements`, `verify`, `recycle_policy`. `pr` is a
        PrPrimitiveSpec dict (`enabled`, `recycle_policy`); omit for the
        default disabled primitive. `budget` is `{"total": float}`
        (default 25.0). Pair with `patch_workflow` for live rollout-knob
        flips that don't need a full re-register."""
        payload: dict[str, Any] = {
            "project": project,
            "name": name,
            "phases": phases,
        }
        if trigger_label is not None:
            payload["trigger_label"] = trigger_label
        if pr is not None:
            payload["pr"] = pr
        if budget is not None:
            payload["budget"] = budget
        if default_requirements is not None:
            payload["default_requirements"] = default_requirements
        return client.post("/v1/workflows", json=payload)

    @mcp.tool()
    def scaffold_workflow(
        project: str,
        name: str,
        runner_image: str | None = None,
    ) -> dict[str, Any]:
        """Emit a starter Glimmung workflow template matching the canonical
        prepare → work → testing → cleanup shape.

        Returns a payload ready to pass to `register_workflow` after
        editing the per-phase scripts/jobs to match the project's
        actual runner. The shape satisfies the mandatory-phase rule
        the registration endpoint enforces (≥1 entry, ≥1 verify or
        evidence-verification gate, ≥1 always teardown). See
        `glimmung/docs/workflow-shape.md` for the rationale.

        `runner_image` is interpolated into every k8s_job phase. Pass
        the project's app-owned runner image; if omitted the template
        uses a placeholder so the call still works as documentation.
        """
        image = runner_image or "REPLACE_ME:runner-image"
        return {
            "project": project,
            "name": name,
            "phases": [
                {
                    "name": "prepare",
                    "kind": "k8s_job",
                    "depends_on": [],
                    "outputs": ["validation_url"],
                    "jobs": [
                        {
                            "id": "prepare",
                            "image": image,
                            "command": ["/bin/bash", "/opt/scripts/prepare.sh"],
                            "steps": [
                                {"slug": "clone-repo", "title": "Clone repo"},
                                {"slug": "deploy-validation-env",
                                 "title": "Deploy validation env"},
                                {"slug": "emit-outputs",
                                 "title": "Emit phase outputs"},
                            ],
                            "timeout_seconds": 1800,
                        },
                    ],
                },
                {
                    "name": "work",
                    "kind": "k8s_job",
                    "depends_on": ["prepare"],
                    "inputs": {
                        "validation_url":
                            "${{ phases.prepare.outputs.validation_url }}",
                    },
                    "jobs": [
                        {
                            "id": "work",
                            "image": image,
                            "command": ["/bin/bash", "/opt/scripts/work.sh"],
                            "steps": [
                                {"slug": "implement", "title": "Implement"},
                                {"slug": "push-branch", "title": "Push branch"},
                            ],
                            "timeout_seconds": 5400,
                        },
                    ],
                },
                {
                    "name": "testing",
                    "kind": "k8s_job",
                    "depends_on": ["work"],
                    "verify": True,
                    "outputs": ["verification"],
                    "inputs": {
                        "validation_url":
                            "${{ phases.prepare.outputs.validation_url }}",
                    },
                    "jobs": [
                        {
                            "id": "testing",
                            "image": image,
                            "command": ["/bin/bash", "/opt/scripts/testing.sh"],
                            "steps": [
                                {"slug": "run-tests", "title": "Run tests"},
                                {"slug": "emit-verdict",
                                 "title": "Emit verification verdict"},
                            ],
                            "timeout_seconds": 1800,
                        },
                    ],
                },
                {
                    "name": "cleanup",
                    "kind": "k8s_job",
                    "always": True,
                    "jobs": [
                        {
                            "id": "cleanup",
                            "image": image,
                            "command": ["/bin/bash", "/opt/scripts/cleanup.sh"],
                            "steps": [
                                {"slug": "teardown",
                                 "title": "Tear down validation env"},
                            ],
                            "timeout_seconds": 600,
                        },
                    ],
                },
            ],
            "pr": {"enabled": True},
            "budget": {"total": 25.0},
        }

    @mcp.tool()
    def patch_workflow(
        project: str,
        name: str,
        pr_enabled: bool | None = None,
        budget_total: float | None = None,
    ) -> dict[str, Any]:
        """Patch Glimmung workflow rollout knobs such as PR creation and budget.

        Patch a Workflow's live rollout knobs (`pr.enabled`, `budget.total`).
        All fields optional — None means "don't change". Structural fields
        (phases, recycle policy) are not patchable here; re-run
        register_workflow for those.

        `name` is the workflow's canonical handle (e.g. "agent-run"); pair
        it with `project` (the partition key)."""
        payload: dict[str, Any] = {}
        if pr_enabled is not None:
            payload["pr_enabled"] = pr_enabled
        if budget_total is not None:
            payload["budget_total"] = budget_total
        return client.patch(f"/v1/workflows/{project}/{name}", json=payload)

    @mcp.tool()
    def check_workflow_updates(
        project: str,
        workflow: str,
        ref: str = "main",
    ) -> dict[str, Any]:
        """Check whether a project repo's `.glimmung/workflows/<workflow>.yaml`
        differs from what's currently registered for that workflow in
        Glimmung. Read-only — does not change anything in Glimmung.

        Returns a `WorkflowUpstreamResult` shape:
          {
            "project": ..., "workflow": ..., "ref": ..., "repo": ...,
            "upstream": <WorkflowRegister payload from the file>,
            "current":  <Workflow currently registered, or null>,
            "in_sync":  bool — True only if both exist and match,
          }

        Use this before `sync_workflow` when you want to see what would
        change. The `ref` parameter overrides the default branch (`main`)
        — set it to inspect a feature branch's proposed workflow shape
        before merging."""
        return client.get(
            f"/v1/projects/{project}/workflows/{workflow}/upstream",
            params={"ref": ref},
        )

    @mcp.tool()
    def sync_workflow(
        project: str,
        workflow: str,
        ref: str = "main",
    ) -> dict[str, Any]:
        """Apply a project repo's `.glimmung/workflows/<workflow>.yaml` to
        Glimmung — fetch upstream, validate, upsert when different.
        Idempotent: calling on an already-in-sync workflow is a no-op
        that still returns the comparison result.

        Use this after pushing a workflow-shape change to main so the new
        definition takes effect without anybody running register-workflow
        scripts. The returned `WorkflowUpstreamResult.in_sync` will always
        be True on success."""
        return client.post(
            f"/v1/projects/{project}/workflows/{workflow}/sync",
            params={"ref": ref},
        )

    @mcp.tool()
    def patch_issue(
        project: str,
        issue_number: int,
        title: str | None = None,
        body: str | None = None,
        labels: list[str] | None = None,
        state: str | None = None,
    ) -> dict[str, Any]:
        """Patch or update a Glimmung issue by project-scoped issue number.

        All fields optional — None means \"don't change\".
        Pass an empty string to actually clear `body`, or an empty list to
        clear `labels`. `state` is \"open\" or \"closed\"; transitions route
        through close_issue / reopen_issue so closed_at is stamped
        consistently."""
        payload: dict[str, Any] = {}
        if title is not None:
            payload["title"] = title
        if body is not None:
            payload["body"] = body
        if labels is not None:
            payload["labels"] = labels
        if state is not None:
            payload["state"] = state
        return client.patch(f"/v1/issues/by-number/{project}/{issue_number}", json=payload)

    @mcp.tool()
    def archive_issue(
        project: str,
        issue_number: int,
        reason: str = "",
    ) -> dict[str, Any]:
        """Archive a Glimmung issue.

        Archives are implemented by closing the issue and adding an audit
        comment. Closed issues are omitted from list_issues by default."""
        return client.post(
            f"/v1/issues/by-number/{project}/{issue_number}/archive",
            json={"reason": reason},
        )

    @mcp.tool()
    def discard_issue(
        project: str,
        issue_number: int,
        reason: str = "",
    ) -> dict[str, Any]:
        """Discard a Glimmung issue.

        Discards are implemented by closing the issue and adding an audit
        comment. Use for issues that should leave the active queue without
        implying completed work."""
        return client.post(
            f"/v1/issues/by-number/{project}/{issue_number}/discard",
            json={"reason": reason},
        )

    @mcp.tool()
    def create_issue(
        project: str,
        title: str,
        body: str = "",
        labels: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a Glimmung-native issue.

        The returned `ref` is the canonical handle for detail, comments,
        and dispatch APIs."""
        return client.post(
            "/v1/issues",
            json={
                "project": project,
                "title": title,
                "body": body,
                "labels": labels or [],
            },
        )

    @mcp.tool()
    def enqueue_signal(
        target_type: str,
        target_repo: str,
        target_ref: str,
        source: str = "glimmung_ui",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Enqueue a Glimmung signal for an issue, pull request (PR), or run.

        Use to feed actionable feedback or trigger details into the drain loop.
        Common values:
        `target_type` is `pr`, `issue`, or `run`; `target_repo` is the
        repository slug / partition key; `target_ref` is the public PR,
        issue, or run handle, such as `owner/repo#42`, `project#17`, or
        `project#17/runs/2`. Put the actionable feedback or trigger detail
        in `payload`."""
        return client.post(
            "/v1/signals",
            json={
                "target_type": target_type,
                "target_repo": target_repo,
                "target_ref": target_ref,
                "source": source,
                "payload": payload or {},
            },
        )

    @mcp.tool()
    def replay_run_decision(
        project: str,
        issue_number: int,
        run_number: str,
        synthetic_completion: dict[str, Any],
        override_workflow: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Replay the Glimmung run decision engine without writes or dispatch.

        Use to debug workflow verification, phase outputs, retry/recycle
        decisions, PR opening decisions, and registration fixes. This is a
        pure-function replay with no Cosmos writes and no GHA dispatch.
        Returns the decision the
        engine *would* make for `synthetic_completion`, plus a next-action
        hint (which phase would advance, which recycle target would fire,
        what abort comment would be posted).

        Smoke-test substrate from glimmung#111: catches verify=true→false-
        class registration bugs at zero cost. The classic case — registered
        verify=true, /completed callback omits the verification field —
        used to cost ~20 min of agent runtime per iteration to surface;
        replay returns ABORT_MALFORMED in milliseconds.

        `synthetic_completion` mirrors the live `/completed` callback body:
        `{conclusion: "success"|"failure"|..., verification: dict|null,
        phase_outputs: dict|null}`. Copy-paste a real completion and tweak
        fields to ask "what if?".

        `run_number` is the issue-scoped run display number, so retry cycles
        like "14.3" are addressable. `override_workflow` is optional. When
        set, the replay uses the provided shape instead of the live
        registration — useful for previewing a registration fix before
        applying it. Shape:
        `{phases: [...PhaseSpec...], pr: {...}, budget: {...}}`. Cross-
        phase input refs are validated; a typo in
        `${{ phases.X.outputs.Y }}` 422s with the same error
        register_workflow returns.

        Returns: `{decision, applied_to_phase, applied_to_attempt_index,
        abort_reason?, would_advance_to_phase?, would_open_pr,
        would_retry_target_phase?, cumulative_cost_usd_after,
        attempts_in_phase_after, workflow_source}`. `workflow_source` is
        "registered" or "override" so the verdict's basis is unambiguous.
        """
        run_number = _run_display(run_number)
        payload: dict[str, Any] = {"synthetic_completion": synthetic_completion}
        if override_workflow is not None:
            payload["override_workflow"] = override_workflow
        return client.post(
            f"/v1/projects/{project}/issues/{issue_number}/runs/{run_number}/replay",
            json=payload,
        )

    @mcp.tool()
    def resume_run(
        project: str,
        issue_number: int,
        run_number: str,
        entrypoint_phase: str,
        entrypoint_job_id: str | None = None,
        entrypoint_step_slug: str | None = None,
        input_overrides: dict[str, str] | None = None,
        artifact_refs: dict[str, str] | None = None,
        context: dict[str, Any] | None = None,
        trigger_source: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Resume a Glimmung run by spawning a new run from a terminal prior run at a chosen phase.

        Use to re-drive failed or aborted multi-phase workflows without
        re-running successful earlier phases. Picks up at `entrypoint_phase`.
        All phases declared earlier in the workflow
        order are auto-skipped — each gets a synthesized PhaseAttempt
        with `phase_outputs` carried forward from the prior Run's same-
        named phase, and the multi-phase substitution path feeds those
        outputs into the entrypoint phase's `workflow_dispatch.inputs`.

        The motivating case from glimmung#111: an `agent-execute`
        attempt aborted on a `verify=true→false` registration mismatch.
        After fixing the registration, `resume_run(... entrypoint_phase=
        "agent-execute")` re-uses `env-prep`'s captured outputs and
        dispatches a fresh `agent-execute` attempt without re-running
        env-prep — saves ~20 minutes of agent runtime per iteration.

        Refuses with state=`prior_in_progress` if the prior Run is
        still IN_PROGRESS (would race the in-flight dispatch's lock).
        Refuses with state=`already_running` if the issue's lock is
        currently held by a different Run (caller must abort the
        conflicting run first).

        For native `k8s_job` phases, set `entrypoint_job_id` and
        `entrypoint_step_slug` to restart at a specific app-owned step
        boundary. Earlier jobs/steps are pre-marked skipped on the new
        run, and the boundary plus `artifact_refs` / `context` are exposed
        to the native pod via GLIMMUNG_* env vars. `input_overrides`
        replaces substituted phase input values for the resumed attempt.

        `trigger_source` is recorded on the new Run for observability;
        the server adds `kind: resume_via_mcp` and `resumed_from_run_number`
        if not provided.

        Returns: `{state, new_run_ref, prior_run_ref, lease?, host?,
        detail?}`. State values include
        `dispatched`, `pending`, `dispatch_failed`, `prior_in_progress`,
        `already_running`, `phase_invalid`, `outputs_missing`,
        `prior_missing`, `workflow_missing`. The HTTP layer maps the
        validation states to 4xx; happy paths return state in the body.
        """
        run_number = _run_display(run_number)
        ts: dict[str, Any] = {
            "kind": "resume_via_mcp",
            "resumed_from_issue_number": issue_number,
            "resumed_from_run_number": run_number,
        }
        if trigger_source:
            ts.update(trigger_source)
        payload: dict[str, Any] = {
            "entrypoint_phase": entrypoint_phase,
            "trigger_source": ts,
        }
        for k, v in {
            "entrypoint_job_id": entrypoint_job_id,
            "entrypoint_step_slug": entrypoint_step_slug,
            "input_overrides": input_overrides,
            "artifact_refs": artifact_refs,
            "context": context,
        }.items():
            if v:
                payload[k] = v
        return _hide_lease_id(
            client.post(
                f"/v1/projects/{project}/issues/{issue_number}/runs/{run_number}/resume",
                json=payload,
            )
        )

    @mcp.tool()
    def abort_run(
        project: str,
        issue_number: int,
        run_number: str,
        reason: str = "aborted_via_mcp",
    ) -> dict[str, Any]:
        """Abort a Glimmung run by issue-scoped run display number.

        Use for orphaned, stuck, or intentionally cancelled runs. Flips a Run
        from in_progress to aborted and releases any locks it was holding.
        Use when a Run has no lease or workflow_run_id and `cancel_lease`
        can't grip onto it. Pass retry cycles as strings, e.g. `run_number="14.3"`.

        Idempotent — calling twice returns `state: already_terminal` the
        second time. If the Run has a workflow_run_id, a GH cancel is
        POSTed best-effort; `gh_run_cancelled` records the outcome
        (`None` if no GH dispatch was attempted)."""
        run_number = _run_display(run_number)
        return client.post(
            f"/v1/projects/{project}/issues/{issue_number}/runs/{run_number}/abort",
            params={"reason": reason},
        )

    @mcp.tool()
    def dispatch_run(
        issue_number: int,
        project: str,
        workflow: str | None = None,
    ) -> dict[str, Any]:
        """Dispatch a Glimmung agent run for an issue and workflow.

        Use to manually start or re-drive a run after a fix lands. Same path
        the dashboard's re-dispatch button takes: claims a host that matches
        the workflow's requirements, creates a Run, and fires the
        workflow_dispatch event or the first phase of a multi-phase workflow.

        `issue_number` is the project-scoped issue number, e.g.
        `project="glimmung", issue_number=141`. `workflow` is optional and
        only needed if the project has more than one workflow registered.

        Returns the dispatch result: created run number, claimed lease label,
        host, and the GHA workflow_dispatch outcome."""
        payload: dict[str, Any] = {
            "project": project,
            "issue_number": issue_number,
        }
        if workflow is not None:
            payload["workflow"] = workflow
        return _hide_lease_id(client.post("/v1/runs/dispatch", json=payload))

    @mcp.tool()
    def checkout_test_slot(
        project: str,
        tank_session_id: str,
        workflow: str | None = None,
        ttl_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Reserve a Glimmung native app test slot.

        Use this when you need an ad-hoc app slot. Glimmung chooses an
        available slot, records a native lease, and starts lease-scoped runtime
        activation. Callers cannot select a slot or request clean-slate
        cleanup through checkout; queue-size changes and returns own
        destructive cleanup.

        Checkout may return while activation is still in progress. In that
        case the response includes `state: "activating"`, `usable: false`, the
        assigned slot name/index, and `status_url`; poll that status URL or
        `get_state` until the slot is `active` and `usable` before relying on
        the environment.

        `tank_session_id` is required so Tank's UI can mark the requesting
        session with the leased environment number and URL."""
        normalized_tank_session_id = _tank_session_id(tank_session_id)
        requester = {
            "consumer": "tank-operator",
            "kind": "tank_session",
            "ref": f"tank-operator/session/{normalized_tank_session_id}",
            "label": normalized_tank_session_id,
            "metadata": {"tank_session_id": normalized_tank_session_id},
        }
        payload: dict[str, Any] = {
            "project": project,
            "requester": requester,
            "tank_session_id": normalized_tank_session_id,
        }
        if workflow is not None:
            payload["workflow"] = workflow
        if ttl_seconds is not None:
            payload["ttl_seconds"] = ttl_seconds
        result = client.post("/v1/test-slots/checkout", json=payload)
        tank_state = None
        if (
            tank_client is not None
            and result.get("state") in {"activating", "active", "claimed"}
            and result.get("slot_index") is not None
        ):
            slot_url = result.get("url")
            if not isinstance(slot_url, str) or not slot_url:
                tank_state = {"error": "checkout response did not include a test slot url"}
            else:
                try:
                    tank_state = tank_client.set_test_environment(
                        _pod_ip(),
                        session_id=normalized_tank_session_id,
                        active=True,
                        slot_index=result.get("slot_index"),
                        url=slot_url,
                    )
                except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                    tank_state = {"error": str(exc)}
        sanitized = _hide_lease_id(result)
        if tank_state is not None:
            if "error" in tank_state:
                sanitized["tank_test_state_error"] = tank_state["error"]
            else:
                sanitized["tank_test_state"] = tank_state.get("test_state")
                sanitized["tank_session_url"] = tank_state.get("url")
        return sanitized

    @mcp.tool()
    def return_test_slot(
        project: str,
        slot_index: int | None = None,
        slot_name: str | None = None,
        tank_session_id: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Return a checked-out Glimmung native app test slot.

        Returning a test slot tells Glimmung the caller no longer needs the
        leased environment. The server tears down the slot namespace for
        active test-slot checkouts, then releases the reservation. Use
        `slot_index` or `slot_name` for normal MCP use. Pass `tank_session_id`
        to clear Tank's GUI test pill for the session. Pass `reason` when the
        return is administrative or otherwise non-obvious."""
        payload: dict[str, Any] = {"project": project}
        if slot_index is not None:
            payload["slot_index"] = slot_index
        if slot_name is not None:
            payload["slot_name"] = slot_name
        caller_pod_ip = current_caller_pod_ip()
        if caller_pod_ip:
            payload["caller_pod_ip"] = caller_pod_ip
        if tank_session_id is not None:
            payload["caller_session_id"] = _tank_session_id(tank_session_id)
        if reason is not None:
            payload["reason"] = reason
        payload["source"] = "mcp-glimmung.return_test_slot"
        log.info(
            "mcp tool return_test_slot project=%s slot_index=%s slot_name=%s "
            "tank_session_id=%s caller_pod_ip=%s reason=%s",
            project,
            slot_index,
            slot_name,
            _tank_session_id(tank_session_id) if tank_session_id is not None else None,
            caller_pod_ip,
            reason,
        )
        result = client.post("/v1/test-slots/return", json=payload)
        tank_state = None
        if tank_client is not None and tank_session_id is not None:
            try:
                tank_state = tank_client.set_test_environment(
                    _pod_ip(),
                    session_id=_tank_session_id(tank_session_id),
                    active=False,
                )
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                tank_state = {"error": str(exc)}
        sanitized = _hide_lease_id(result)
        if tank_state is not None:
            if "error" in tank_state:
                sanitized["tank_test_state_error"] = tank_state["error"]
            else:
                sanitized["tank_test_state"] = tank_state.get("test_state")
                sanitized["tank_session_url"] = tank_state.get("url")
        return sanitized

    @mcp.tool()
    def repair_test_slot(project: str, slot_name: str) -> dict[str, Any]:
        """Repair/revalidate one configured, unleased Glimmung native test slot.

        Use this for admin repair of prepared capacity when a configured slot
        is missing preliminary resources or has a preliminary provisioning
        error. The server rejects active leased/runtime slots, marks the slot
        `provisioning`, reruns preliminary reconciliation and the warm Helm
        pass only, then returns the slot to `provisioned` or records `error`.
        This does not change queue size and does not activate hot runtime."""
        log.info(
            "mcp tool repair_test_slot project=%s slot_name=%s",
            project,
            slot_name,
        )
        return _hide_lease_id(
            client.post(f"/v1/projects/{project}/test-environments/{slot_name}/repair")
        )

    @mcp.tool()
    def extend_test_slot_lease(
        project: str,
        tank_session_id: str,
        extend_seconds: int = 3600,
        slot_index: int | None = None,
        slot_name: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Extend the TTL on a checked-out Glimmung native app test slot.

        Use this when the current Tank session still needs its leased test
        environment. The server updates the durable lease TTL and re-arms the
        test-slot expiry timer, so the extension survives Glimmung restarts.
        Pass `tank_session_id` to prove the session owns the checkout. Use
        `slot_index` or `slot_name` only when the session may hold more than
        one slot or the target should be explicit."""
        normalized_tank_session_id = _tank_session_id(tank_session_id)
        payload: dict[str, Any] = {
            "project": project,
            "tank_session_id": normalized_tank_session_id,
            "extend_seconds": extend_seconds,
            "source": "mcp-glimmung.extend_test_slot_lease",
        }
        if slot_index is not None:
            payload["slot_index"] = slot_index
        if slot_name is not None:
            payload["slot_name"] = slot_name
        caller_pod_ip = current_caller_pod_ip()
        if caller_pod_ip:
            payload["caller_pod_ip"] = caller_pod_ip
        if reason is not None:
            payload["reason"] = reason
        log.info(
            "mcp tool extend_test_slot_lease project=%s slot_index=%s "
            "slot_name=%s tank_session_id=%s extend_seconds=%s "
            "caller_pod_ip=%s reason=%s",
            project,
            slot_index,
            slot_name,
            normalized_tank_session_id,
            extend_seconds,
            caller_pod_ip,
            reason,
        )
        return _hide_lease_id(client.post("/v1/test-slots/extend", json=payload))
