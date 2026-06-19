"""mcp-glimmung tools — typed wrappers over glimmung's HTTP API.

Read surface plus session-safe mutations. Lease and webhook endpoints stay
unexposed — those are runner / orchestrator concerns, not session concerns.
"""

import json
import logging
import os
import time
from typing import Any
from urllib.parse import urlsplit

import httpx
from mcp.server.fastmcp import FastMCP
from romaine_auth import current_caller

from .browser_inspector import (
    fresh_inspection_request_id,
    inspect_url,
    summary_view,
)
from .caller import current_tank_session_scope, require_tank_session_id
from .glimmung_client import GlimmungClient
from .tank_client import TankClient

log = logging.getLogger(__name__)
_TANK_AUTH_STORAGE_KEY = "auth-romaine-jwt"


def _tank_session_id(value: str) -> str:
    """Normalize either a Tank session id or its Kubernetes pod name."""
    return value.removeprefix("session-")


def _origin_from_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("tank_auth requires an absolute http(s) URL")
    return f"{parsed.scheme}://{parsed.netloc}"


def _caller_raw_token() -> str:
    caller = current_caller()
    token = str(getattr(caller, "raw_token", "") or "").strip()
    if not token:
        raise RuntimeError(
            "tank_auth requires the inbound auth.romaine.life caller token; "
            "CallerJWTMiddleware should have bound current_caller().raw_token"
        )
    return token


def _preflight_tank_auth(origin: str, token: str) -> dict[str, Any]:
    url = f"{origin}/api/auth/me"
    try:
        response = httpx.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        raise RuntimeError(f"tank_auth preflight failed for {url}: {exc}") from exc
    if not response.is_success:
        body = (response.text or "").strip()
        if len(body) > 500:
            body = body[:500] + "...(truncated)"
        detail = f": {body}" if body else ""
        raise RuntimeError(
            f"tank_auth preflight failed: GET {url} -> {response.status_code}{detail}"
        )
    try:
        user = response.json()
    except ValueError as exc:
        raise RuntimeError(f"tank_auth preflight returned non-JSON from {url}") from exc
    if not isinstance(user, dict):
        raise RuntimeError(f"tank_auth preflight returned unexpected body from {url}")
    return {
        "mode": "tank_caller",
        "preflight_status": response.status_code,
        "email": user.get("email"),
        "role": user.get("role"),
        "is_admin": user.get("is_admin"),
        "sub": user.get("sub"),
        "installation_id": user.get("installation_id"),
    }


def _with_tank_auth_local_storage(
    local_storage: dict[str, dict[str, str]] | None,
    origin: str,
    token: str,
) -> dict[str, dict[str, str]]:
    if local_storage is not None and (
        not isinstance(local_storage, dict)
        or any(not isinstance(k, str) or not isinstance(v, dict) for k, v in local_storage.items())
    ):
        raise ValueError("local_storage must be a dict of origin -> dict of str -> str")

    merged: dict[str, dict[str, str]] = {
        storage_origin: dict(items)
        for storage_origin, items in (local_storage or {}).items()
    }
    origin_items = merged.setdefault(origin, {})
    existing = origin_items.get(_TANK_AUTH_STORAGE_KEY)
    if existing is not None and existing != token:
        raise ValueError(
            "tank_auth=True conflicts with local_storage auth-romaine-jwt for "
            f"{origin}; remove the manual token or set tank_auth=False"
        )
    origin_items[_TANK_AUTH_STORAGE_KEY] = token
    return merged


def _lease_label(lease: dict[str, Any]) -> str:
    number = lease.get("lease_number")
    if number is not None:
        return f"#{number}"
    metadata = lease.get("metadata") if isinstance(lease.get("metadata"), dict) else {}
    slot_name = metadata.get("runner_slot_name")
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
    """Validate and return a canonical run-cycle address (``run.cycle``).

    A run cycle is addressed by its display number: the logical run number and
    the run-local cycle ordinal, e.g. ``"6.1"``. A bare run number or the flat
    issue-scoped cycle-ledger number is a *display* value, not an address —
    glimmung's resolvers reject it (and used to silently resolve it to the
    wrong run cycle). Mirrors ParseRunCycleAddress in glimmung; see
    romaine-life/glimmung docs/run-graph-display-design.md.
    """
    display = str(value).strip()
    if not display:
        raise ValueError("run_number required")
    parts = display.split(".")
    if (
        len(parts) == 2
        and parts[0].isdigit()
        and parts[1].isdigit()
        and int(parts[0]) >= 1
        and int(parts[1]) >= 1
    ):
        return display
    raise ValueError(
        f'run_number must be the canonical run-cycle number "run.cycle" '
        f'(e.g. "6.1"); got {display!r}. A bare run number or the flat '
        f"issue-scoped cycle-ledger number is a display value, not an address."
    )


def _dashboard_path(value: str) -> str:
    """Extract the absolute dashboard path from a Glimmung dashboard URL.

    Accepts a full URL (``https://glimmung.romaine.life/projects/...``) or an
    absolute path (``/projects/...``); query strings and fragments are dropped
    and the host is stripped, since the path is re-hosted against the configured
    glimmung backend. The path must address the dashboard ``/projects`` surface;
    anything else is rejected so a mistyped link can't hit an unrelated
    endpoint. The URL-to-resource resolution itself — including canonical
    run-cycle addressing — happens server-side; see glimmung
    publicids.ParseDashboardPath.
    """
    text = (value or "").strip()
    if not text:
        raise ValueError("dashboard url must not be empty")
    if "://" in text or text.startswith("/"):
        path = urlsplit(text).path
    else:
        raise ValueError(
            f"expected a Glimmung dashboard URL or absolute path, got {value!r}"
        )
    path = path.rstrip("/") or "/"
    if path != "/projects" and not path.startswith("/projects/"):
        raise ValueError(
            f"not a Glimmung dashboard resource path: {path!r} (expected /projects/...)"
        )
    return path


def _resolve_slot_playwright_ws(client: GlimmungClient, tank_session_id: str) -> str:
    """Find the active test-slot lease for a Tank session and return its
    slot-playwright Service ws endpoint."""
    endpoint, _project = _resolve_slot_playwright_ws_and_project(client, tank_session_id)
    return endpoint


def _resolve_slot_playwright_ws_and_project(
    client: GlimmungClient, tank_session_id: str
) -> tuple[str, str]:
    """Find the active test-slot lease for a Tank session and return both
    its slot-playwright Service ws endpoint and the lease's project.

    Errors if the session does not currently hold an active test-slot lease,
    or if the lease's slot does not yet expose a playwright endpoint
    (slot still activating, or the cluster is not running playwright-enabled
    slots). A test slot must already be provisioned for the session (the Tank
    Test button/endpoint provisions one deterministically server-side) before
    calling `inspect_browser_url`.
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
        # (2) and (3) are what the runner-k8s test-slot allocator writes
        # today — (1) was never populated by the slot checkout path, so the
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
        project = lease.get("project") or metadata.get("project") or ""
        if not isinstance(project, str):
            project = ""
        if isinstance(endpoint, str) and endpoint:
            return endpoint, project
        raise RuntimeError(
            f"test slot for tank session {tank_session_id!r} has no "
            "playwright_ws_endpoint yet; slot may still be activating or "
            "glimmung is not running playwright-enabled slots"
        )
    raise RuntimeError(
        f"no active test-slot lease found for tank session {tank_session_id!r}; "
        "provision a test slot (via the Tank Test button/endpoint) before "
        "inspect_browser_url"
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
        """Get one RunReport by issue-scoped run-cycle number.

        ``run_number`` is the canonical ``run.cycle`` display number: "run 1.3
        for glimmung#141" maps to `project="glimmung", issue_number=141,
        run_number="1.3"`. A bare run number or the flat issue-scoped
        cycle-ledger number is not an address and is rejected — the ledger
        number is a display value only.
        """
        run_number = _run_display(run_number)
        return client.get(
            f"/v1/projects/{project}/issues/{issue_number}/runs/{run_number}/report",
        )

    @mcp.tool()
    def get_dashboard_resource(url: str) -> dict[str, Any]:
        """Resolve a Glimmung dashboard URL to its canonical resource JSON.

        Paste a dashboard deep link exactly as it appears in the browser — a
        full URL or the ``/projects/...`` path, e.g.
        ``https://glimmung.romaine.life/projects/ambience/issues/168/runs/9/cycles/1/phases/llm-verify/jobs/llm-verify/steps/run-verification``
        — and get the resource back without dissecting the URL yourself:

        - a run, or any phase/job/step under it, returns the ``RunReport`` (the
          canonical review object) wrapped with a ``focus`` block naming the
          addressed phase/job/step and ``links`` to the typed reads — the run
          report, runner events narrowed to that job/step, and runner status;
        - an issue link returns the issue detail.

        The server owns the URL-to-resource resolution, including canonical
        run-cycle addressing, so a copied link Just Works. Project and
        run-index links are navigation surfaces, not single resources, and
        return an error pointing at a specific issue or run.
        """
        return client.get(_dashboard_path(url), params={"format": "json"})

    @mcp.tool()
    def get_runner_events(
        project: str,
        issue_number: int,
        run_number: str,
        attempt_index: int | None = None,
        job_id: str | None = None,
        limit: int | None = 200,
    ) -> dict[str, Any]:
        """Read hot runner k8s_job step/log events for a Glimmung run.

        Use with graph attempt metadata (`phase_kind == "k8s_job"`) to inspect
        the ordered runner event stream. `attempt_index` narrows to one
        PhaseAttempt, `job_id` narrows to one runner job, and `limit` caps the
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
            f"/v1/projects/{project}/issues/{issue_number}/runs/{run_number}/run/events",
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
        """Check checkout availability: runner test slots, free hosts, active leases, and pending leases.

        Returns three lists:
        - `available_test_slots`: runner test slots currently eligible for checkout.
        - `prepared_test_slots`: runner test slots with lifecycle state `available`.
        - `available_hosts`: non-drained registered worker hosts with no current lease.
        - `active_leases`: leases currently holding a host or runner slot.
        - `pending_leases`: leases queued but not yet assigned capacity.
        - `test_slot_admissions`: per-project durable checkout capacity counters.

        Pass `project` to narrow all three lists to a single project.
        Omit it to see the full cross-project picture."""
        state = _sanitize_state_for_sessions(client.get("/v1/state"))

        hosts = state.get("hosts") or []
        test_slots = state.get("test_environments") or []
        admissions = state.get("test_slot_admissions") or []
        active = state.get("active_leases") or []
        pending = state.get("pending_leases") or []

        if project is not None:
            test_slots = [slot for slot in test_slots if slot.get("project") == project]
            admissions = [
                admission for admission in admissions
                if admission.get("project") == project
            ]
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
        prepared_test_slots = [
            slot for slot in test_slots
            if slot.get("state") == "available"
        ]
        checkout_slots_by_project: dict[str, int] = {
            str(admission.get("project")): int(admission.get("checkout_available_test_slots") or 0)
            for admission in admissions
            if admission.get("project") is not None
        }
        seen_checkout_by_project: dict[str, int] = {}
        checkout_available_test_slots = []
        for slot in prepared_test_slots:
            slot_project = str(slot.get("project") or "")
            limit = checkout_slots_by_project.get(slot_project, len(prepared_test_slots))
            seen = seen_checkout_by_project.get(slot_project, 0)
            if seen >= limit:
                continue
            checkout_available_test_slots.append(slot)
            seen_checkout_by_project[slot_project] = seen + 1

        return {
            "available_test_slots": checkout_available_test_slots,
            "prepared_test_slots": prepared_test_slots,
            "available_hosts": available_hosts,
            "active_leases": active,
            "pending_leases": pending,
            "test_slot_admissions": admissions,
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
        viewport: dict[str, int] | None = None,
        wait_ms: int = 2000,
        timeout_ms: int = 30000,
        full_page: bool = True,
        capture_accessibility: bool = False,
        capture_console: bool = True,
        capture_network: bool = True,
        max_elements: int = 80,
        body_text_limit: int = 4000,
        max_console_messages: int = 50,
        max_network_events: int = 50,
        cookies: list[dict[str, Any]] | None = None,
        extra_http_headers: dict[str, str] | None = None,
        local_storage: dict[str, dict[str, str]] | None = None,
        tank_auth: bool = False,
        save_screenshot_to_workspace: bool = False,
        workspace_screenshot_name: str | None = None,
    ) -> dict[str, Any]:
        """Inspect a live URL with Chromium and return a summary plus
        durable artifact URLs.

        Runs inside the active test slot's `slot-playwright` pod, so the
        session must already hold a provisioned test slot (the Tank Test
        button/endpoint provisions one deterministically server-side) first.
        The slot's Playwright drives the browser; the MCP host does not. The
        Tank session lease is derived from trusted caller context, not from a
        model-supplied argument.

        The screenshot PNG and the full structured inspection report are
        uploaded to glimmung via `POST /v1/inspections` and live under
        `inspections/<lease_id>/<inspection_id>/{report.json, screenshot.png}`.
        The tool response is a compact summary that references those
        artifacts plus a bounded preview of the body text, elements list,
        console messages, page errors, and network errors. The full lists
        always live in `report.json` — no data is silently dropped on the
        server side.

        `body_text_limit`, `max_elements`, `max_console_messages`, and
        `max_network_events` control the *summary* size only. The full
        report is unbounded by these parameters.

        Retention: V1 inspections are lease-scoped — both blobs and the
        `slot_inspections` ledger row are deleted at lease cleanup
        (return, callback release, TTL expiry, admin cancel). For
        durable evidence, attach the artifact URL through the existing
        run/touchpoint evidence machinery; the `pr_touchpoint` finalize
        step canonicalizes `inspections/` refs into Touchpoint evidence
        without any new caller-facing promotion API.

        When `save_screenshot_to_workspace=True`, the screenshot PNG is also
        uploaded into the caller Tank session's workspace through Tank's
        normal file upload endpoint. Tank stores image uploads under
        `/workspace/screenshots/` and returns the exact path in
        `workspace_screenshot`. Use `workspace_screenshot_name` to set the
        uploaded file name for labeling/extension purposes; Tank still owns
        the collision-safe final path.

        Auth-injection parameters drive an *authenticated* browse — the
        slot-playwright pod holds no credentials of its own, so the
        caller is the only source of identity. Four knobs are available:

        - `tank_auth`: when true, use the already-verified inbound
          auth.romaine.life caller JWT bound by mcp-auth-proxy, preflight
          it against the inspected URL's `/api/auth/me`, then seed it into
          `localStorage[auth-romaine-jwt]` for that URL's origin. This is
          the preferred Tank UI path: no model-visible JWT copy/paste and
          no projected service-account exchange inside mcp-glimmung.
        - `cookies`: list of Playwright cookie dicts, applied via
          `context.addCookies`.
        - `extra_http_headers`: dict applied via
          `context.setExtraHTTPHeaders`. Useful for `Authorization:
          Bearer …` on slot URLs that hit JSON APIs.
        - `local_storage`: dict of `origin -> {key: value}`. Seeded by
          an `addInitScript` that runs before every page script, so
          SPAs that boot from `localStorage[auth-romaine-jwt]` come
          up already signed in.

        Detailed schema validation (sameSite enum, url vs. domain
        exclusivity, etc.) is delegated to Playwright: its error text
        is more precise than anything this wrapper can pre-validate
        and it bubbles up through the subprocess stderr.
        """
        session_id = require_tank_session_id()
        if save_screenshot_to_workspace and tank_client is None:
            raise RuntimeError(
                "save_screenshot_to_workspace requires a TankClient; "
                "mcp-glimmung must be configured with Tank's internal API"
            )
        ws_endpoint, project = _resolve_slot_playwright_ws_and_project(client, session_id)
        auth_diagnostic: dict[str, Any] | None = None
        if tank_auth:
            origin = _origin_from_url(url)
            caller_token = _caller_raw_token()
            auth_diagnostic = _preflight_tank_auth(origin, caller_token)
            local_storage = _with_tank_auth_local_storage(
                local_storage,
                origin,
                caller_token,
            )
        report = inspect_url(
            url=url,
            playwright_ws_endpoint=ws_endpoint,
            viewport=viewport,
            wait_ms=wait_ms,
            timeout_ms=timeout_ms,
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
        if auth_diagnostic is not None:
            report["auth"] = auth_diagnostic
        screenshot_path = report.pop("screenshot_path", None)
        if not screenshot_path:
            raise RuntimeError(
                "browser_inspector did not return a screenshot_path; "
                "MJS subprocess shape regressed"
            )
        try:
            with open(screenshot_path, "rb") as fh:
                screenshot_bytes = fh.read()
            if not screenshot_bytes:
                raise RuntimeError("screenshot tempfile is empty")
            workspace_screenshot: dict[str, Any] | None = None
            if save_screenshot_to_workspace:
                upload_name = (workspace_screenshot_name or "inspection-screenshot.png").strip()
                if not upload_name:
                    upload_name = "inspection-screenshot.png"
                assert tank_client is not None
                workspace_screenshot = tank_client.upload_session_file(
                    session_id,
                    name=upload_name,
                    content_type="image/png",
                    data=screenshot_bytes,
                )
            request_id = fresh_inspection_request_id()
            response = client.post_multipart(
                "/v1/inspections",
                data={
                    "tank_session_id": session_id,
                    "project": project,
                },
                files={
                    "report": (
                        "report.json",
                        json.dumps(report).encode("utf-8"),
                        "application/json",
                    ),
                    "screenshot": (
                        "screenshot.png",
                        screenshot_bytes,
                        "image/png",
                    ),
                },
                extra_headers={"X-Inspection-Request-Id": request_id},
            )
        finally:
            try:
                os.unlink(screenshot_path)
            except FileNotFoundError:
                pass
        summary = summary_view(
            report,
            inspection_id=str(response.get("inspection_id") or ""),
            report_url=str(response.get("report_url") or ""),
            screenshot_url=str(response.get("screenshot_url") or ""),
            scope=str(response.get("scope") or ""),
            scope_ref=str(response.get("scope_ref") or ""),
            body_text_limit=body_text_limit,
            max_elements=max_elements,
            max_console_messages=max_console_messages,
            max_network_events=max_network_events,
        )
        if auth_diagnostic is not None:
            summary["auth"] = auth_diagnostic
        if workspace_screenshot is not None:
            summary["workspace_screenshot"] = workspace_screenshot
        return summary

    @mcp.tool()
    def register_workflow(
        project: str,
        name: str,
        phases: list[dict[str, Any]],
        pr: dict[str, Any] | None = None,
        budget: dict[str, Any] | None = None,
        trigger_label: str | None = None,
        default_requirements: dict[str, Any] | None = None,
        dispatch_inputs: list[dict[str, Any]] | None = None,
        vars: dict[str, str] | None = None,
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
        flips that don't need a full re-register.

        `vars` is the registration-owned variable map referenced by phase-
        and job-level `when` conditions (`${{ vars.<key> }}`). Vars are
        workflow identity, not per-dispatch knobs — they name durable facts
        about the shape (e.g. `{"feature_type": "effect"}`) and are part of
        the content-hashed schema. `when` conditions ride inside the phase/
        job dicts in `phases`; the closed grammar and skip semantics
        (server-evaluated, zero compute on skipped legs, skipped outputs
        resolve empty downstream) are documented in
        `glimmung/docs/workflow-shape.md` → "Conditional Phases And Jobs".

        `dispatch_inputs` declares the per-dispatch input contract. Each
        entry is `{name, description?, required?, default?}`. Every
        `${{ inputs.X }}` reference inside any runner job's `checkout.ref`,
        `extra_checkouts[].ref`, or phase `workflow_ref` must name a
        declared input — the server rejects undeclared template refs at
        register time and rejects missing-required / undeclared values at
        dispatch time. A required input may carry a `default` so a no-input
        dispatch succeeds; a non-required input must declare a non-empty
        `default`. See `glimmung/docs/workflow-shape.md` → "Dispatch
        inputs".

        Control values are operator-owned: recycle policies and budget are
        the operator's dials, and re-registering is NOT a license to
        normalize them — never change them without an explicit operator
        instruction. Verification phases must declare `recycle_policy`
        explicitly (max_attempts=1 runs the gate with recycling off);
        registration rejects silence. Operator control pins are enforced
        server-side: a pinned target's incoming value is discarded for the
        pinned value, reported in the response as `pins_enforced` plus
        `control_changes` entries with `action: "pin_enforced"` — check
        them after registering. A pin whose target phase is missing from
        your payload rejects the registration; ask the operator to unpin
        rather than working around it. Every register lands in the
        attributed control ledger (`list_workflow_control_events`)."""
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
        if dispatch_inputs is not None:
            payload["dispatch_inputs"] = dispatch_inputs
        if vars is not None:
            payload["vars"] = vars
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

        A patch naming an operator-pinned control target (e.g. a pinned
        budget) is rejected with the pinner and reason; do not retry —
        the pin is an operator decision, and only an operator unpin
        (`unpin_workflow_control`) releases it.

        `name` is the workflow's canonical handle (e.g. "agent-run"); pair
        it with `project` (the partition key)."""
        payload: dict[str, Any] = {}
        if pr_enabled is not None:
            payload["pr_enabled"] = pr_enabled
        if budget_total is not None:
            payload["budget_total"] = budget_total
        return client.patch(f"/v1/workflows/{project}/{name}", json=payload)

    @mcp.tool()
    def delete_workflow(
        project: str,
        name: str,
    ) -> dict[str, Any]:
        """Deregister (delete) a Glimmung workflow by project + name.

        Permanently removes the workflow definition so it can no longer be
        dispatched. Use this to retire a workflow that has been migrated
        away or replaced — e.g. a dead `gha_dispatch` workflow whose
        GitHub Actions file no longer exists. Admin-gated server-side; the
        caller's identity rides through on the forwarded JWT.

        Returns the deleted Workflow record. Errors if no workflow matches
        `project`/`name`. This is not reversible from the MCP surface —
        re-create with `register_workflow` if needed.

        `name` is the workflow's canonical handle (e.g. "issue-agent");
        pair it with `project` (the partition key)."""
        return client.delete(f"/v1/workflows/{project}/{name}")

    @mcp.tool()
    def pin_workflow_control(
        project: str,
        name: str,
        target: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Pin a workflow control value so re-registrations cannot move it.

        Freezes the CURRENT value at `target` — one of `budget`,
        `pr.recycle_policy`, or `phases.<phase>.recycle_policy`. From then
        on, any `register_workflow` payload carrying a different value for
        the pinned target has it discarded in favor of the pinned value
        (reported as `pins_enforced` / `pin_enforced` control changes), and
        `patch_workflow` calls naming the target are rejected outright.

        Pins freeze what is — to pin a DIFFERENT value, change it first
        (patch/register), then pin. Only pin on explicit operator
        instruction; the pin act is attributed to you in the control
        ledger. `reason` is recorded and shown whenever the pin blocks or
        overrides a write, so make it say why (e.g. "systemic verify fails
        must not recycle — operator decision 2026-06-11")."""
        payload: dict[str, Any] = {}
        if reason is not None and reason.strip():
            payload["reason"] = reason.strip()
        return client.put(
            f"/v1/workflows/{project}/{name}/control-pins/{target}",
            json=payload,
        )

    @mcp.tool()
    def unpin_workflow_control(
        project: str,
        name: str,
        target: str,
    ) -> dict[str, Any]:
        """Release a pinned workflow control value.

        The inverse of `pin_workflow_control`. Unpinning is an explicit,
        attributed act recorded in the control ledger — only do it on
        explicit operator instruction. After unpin, the target is an
        ordinary control again: patchable and replaceable by
        registration."""
        return client.delete(f"/v1/workflows/{project}/{name}/control-pins/{target}")

    @mcp.tool()
    def list_workflow_control_events(
        project: str,
        name: str,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Read a workflow's attributed control ledger (newest first).

        One event per control-plane write — register, patch, pin, unpin,
        delete — with the actor, the resulting schema_ref, and a detail
        document carrying the control diff (budget / pr.recycle_policy /
        per-phase recycle policies) or the pin target+reason. Use this to
        answer "who changed max_attempts and when" without spelunking
        content-addressed schema history (a revert reuses an existing
        schema row; only the ledger records the pointer move)."""
        return client.get(
            f"/v1/workflows/{project}/{name}/control-events",
            params={"limit": limit},
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

        For runner `k8s_job` phases, set `entrypoint_job_id` and
        `entrypoint_step_slug` to restart at a specific app-owned step
        boundary. Earlier jobs/steps are pre-marked skipped on the new
        run, and the boundary plus `artifact_refs` / `context` are exposed
        to the runner pod via GLIMMUNG_* env vars. `input_overrides`
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
        inputs: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Dispatch a Glimmung agent run for an issue and workflow.

        Use to manually start or re-drive a run after a fix lands. Same path
        the dashboard's re-dispatch button takes: claims a host that matches
        the workflow's requirements, creates a Run, and fires the
        workflow_dispatch event or the first phase of a multi-phase workflow.

        `issue_number` is the project-scoped issue number, e.g.
        `project="glimmung", issue_number=141`. `workflow` is optional and
        only needed if the project has more than one workflow registered.
        `inputs` is an optional string map passed to workflow templates such as
        runner checkout refs.

        Returns the dispatch result: created run number, claimed lease label,
        host, and the GHA workflow_dispatch outcome."""
        payload: dict[str, Any] = {
            "project": project,
            "issue_number": issue_number,
        }
        if workflow is not None:
            payload["workflow"] = workflow
        if inputs is not None:
            payload["inputs"] = inputs
        return _hide_lease_id(client.post("/v1/runs/dispatch", json=payload))

    @mcp.tool()
    def synthetic_dispatch_run(
        issue_number: int,
        project: str,
        start_at_phase: str,
        supplied_phase_outputs: list[dict[str, Any]],
        slot_lease_ref: str,
        reason: str,
        workflow: str | None = None,
        copy_phase_outputs_from: dict[str, Any] | None = None,
        namespace: str | None = None,
        validation_url: str | None = None,
        trigger_source: dict[str, Any] | None = None,
        inputs: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Create a break-glass synthetic Glimmung run from caller-supplied facts.

        This tool is intentionally strict and unhelpful. It does not fetch old
        runs unless explicitly told to copy selected phase outputs, infer
        missing outputs, provision a test slot, or decide which phases matter.
        The caller must provide the exact `start_at_phase`, a claimed
        `slot_lease_ref`, and every skipped phase output that the entrypoint
        phase needs. Missing or wrong data should fail at the Glimmung API
        boundary.

        `supplied_phase_outputs` is a list of objects shaped like
        `{"phase": "llm-work", "phase_outputs": {"branch_name": "..."}}`.
        For recovery of already-validated evidence, a verification phase may
        instead include a typed `verification` block such as
        `{"phase": "llm-verify", "verification": {"status": "pass",
        "reasons": ["..."], "evidence_refs": ["runs/.../proof.png"]}}`.
        Non-verification phases render as supplied; typed passing
        verification phases render as carry-forward successes.

        `copy_phase_outputs_from` optionally asks Glimmung to copy selected
        outputs from a prior run on the same issue before applying
        `supplied_phase_outputs`. Shape:
        `{"run": "17.1", "phases": {"llm-verify": ["verification"]}}`.
        Copied phases must be before `start_at_phase`; explicit supplied
        outputs may add missing keys but cannot conflict with copied keys.
        Copying a legacy output named `verification` does not promote it into
        the typed verification contract.

        `inputs` is an optional string map passed to workflow templates such as
        runner checkout refs (e.g. `git_ref`)."""
        payload: dict[str, Any] = {
            "project": project,
            "issue_number": issue_number,
            "start_at_phase": start_at_phase,
            "supplied_phase_outputs": supplied_phase_outputs,
            "execution_context": {"slot_lease_ref": slot_lease_ref},
            "reason": reason,
        }
        if workflow is not None:
            payload["workflow"] = workflow
        if copy_phase_outputs_from is not None:
            payload["copy_phase_outputs_from"] = copy_phase_outputs_from
        if namespace is not None:
            payload["execution_context"]["namespace"] = namespace
        if validation_url is not None:
            payload["execution_context"]["validation_url"] = validation_url
        if trigger_source is not None:
            payload["trigger_source"] = trigger_source
        if inputs is not None:
            payload["inputs"] = inputs
        return _hide_lease_id(client.post("/v1/runs/synthetic-dispatch", json=payload))

    @mcp.tool()
    def return_test_slot(
        project: str,
        slot_index: int | None = None,
        slot_name: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Return a checked-out Glimmung native app test slot, or re-drive
        cleanup for a slot wedged in a cleanup error.

        Returning a test slot tells Glimmung the caller no longer needs the
        leased environment. The server tears down the slot namespace for
        active test-slot checkouts, then releases the reservation. Use
        `slot_index` or `slot_name` for normal MCP use. The caller Tank session
        is derived from trusted context and is used to clear Tank's GUI test
        pill. Pass `reason` when the return is administrative or otherwise
        non-obvious.

        Double duty as the operator cleanup-retry: when addressed by
        `slot_name`/`slot_index` for a slot orphaned in `error` with a
        `cleanup_error` and no live lease — the shape a transient
        cleanup-dependency outage leaves behind (for example the auth token
        exchange being briefly unreachable during a node upgrade) — Glimmung
        re-drives runtime cleanup (`error -> cleaning`) and converges the slot
        back to the available pool, no process restart required. Like a normal
        return, this is asynchronous: it answers `202` with `state: cleaning`;
        poll `/v1/state` (or `get_state`) until the slot reports `available`.
        Ineligible slots (unknown, healthy, or an activation-only `error`
        without a `cleanup_error`, which belongs to `repair_test_slot`) answer
        `404` and are unchanged."""
        payload: dict[str, Any] = {"project": project}
        if slot_index is not None:
            payload["slot_index"] = slot_index
        if slot_name is not None:
            payload["slot_name"] = slot_name
        caller_session_id = require_tank_session_id()
        payload["caller_session_id"] = caller_session_id
        if reason is not None:
            payload["reason"] = reason
        payload["source"] = "mcp-glimmung.return_test_slot"
        log.info(
            "mcp tool return_test_slot project=%s slot_index=%s slot_name=%s "
            "caller_session_id=%s reason=%s",
            project,
            slot_index,
            slot_name,
            caller_session_id,
            reason,
        )
        result = client.post("/v1/test-slots/return", json=payload)
        tank_state = None
        if tank_client is not None:
            try:
                tank_state = tank_client.set_test_environment(
                    session_id=caller_session_id,
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
    def set_test_environment_count(project: str, count: int) -> dict[str, Any]:
        """Scale a project's reserved Glimmung test-slot capacity.

        Use to grow or shrink the pool of warm test slots Glimmung keeps
        provisioned for a project. Lowering the count tears down the
        highest-indexed slots in excess of `count`; raising it kicks off
        provisioning of additional slots. The server enforces 0 <= count
        <= 50 and rejects scale-downs that would evict an actively-leased
        slot.

        Returns the updated project record. The new capacity becomes
        available for test-slot checkout after provisioning settles."""
        if not isinstance(count, int) or count < 0 or count > 50:
            raise ValueError("count must be an integer between 0 and 50")
        log.info(
            "mcp tool set_test_environment_count project=%s count=%s",
            project,
            count,
        )
        return client.patch(
            f"/v1/projects/{project}/test-environments/count",
            json={"count": count},
        )

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
        extend_seconds: int = 3600,
        slot_index: int | None = None,
        slot_name: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Extend the TTL on a checked-out Glimmung native app test slot.

        Use this when the current Tank session still needs its leased test
        environment. The server updates the durable lease TTL and re-arms the
        test-slot expiry timer, so the extension survives Glimmung restarts.
        The Tank session is derived from trusted caller context. Use `slot_index`
        or `slot_name` only when the session may hold more than one slot or the
        target should be explicit."""
        normalized_tank_session_id = require_tank_session_id()
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
        if reason is not None:
            payload["reason"] = reason
        log.info(
            "mcp tool extend_test_slot_lease project=%s slot_index=%s "
            "slot_name=%s tank_session_id=%s extend_seconds=%s "
            "reason=%s",
            project,
            slot_index,
            slot_name,
            normalized_tank_session_id,
            extend_seconds,
            reason,
        )
        return _hide_lease_id(client.post("/v1/test-slots/extend", json=payload))
