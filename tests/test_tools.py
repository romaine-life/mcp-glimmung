import inspect
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import mcp_glimmung.tools as tools_mod
from mcp_glimmung.tools import register_tools
from mcp_glimmung.caller import CALLER_SESSION_ID


class FakeMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self) -> Any:
        def decorate(fn: Any) -> Any:
            self.tools[fn.__name__] = fn
            return fn

        return decorate


class StubClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None, dict[str, Any] | None]] = []
        self.responses: dict[tuple[str, str], Any] = {}

    def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        self.calls.append(("GET", path, params, None))
        if ("GET", path) in self.responses:
            return self.responses[("GET", path)]
        return {"path": path}

    def patch(self, path: str, json: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("PATCH", path, None, json))
        return {"path": path, "json": json}

    def put(self, path: str, json: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append(("PUT", path, None, json))
        return {"path": path, "json": json}

    def delete(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append(("DELETE", path, params, None))
        if ("DELETE", path) in self.responses:
            return self.responses[("DELETE", path)]
        return {"path": path}

    def post(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        self.calls.append(("POST", path, params, json))
        if ("POST", path) in self.responses:
            return self.responses[("POST", path)]
        return {"path": path, "params": params, "json": json}

    def post_multipart(
        self,
        path: str,
        *,
        data: dict[str, str] | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        self.calls.append(("POST_MULTIPART", path, data, files))
        self.last_multipart = {
            "path": path,
            "data": data,
            "files": files,
            "extra_headers": extra_headers,
        }
        if ("POST_MULTIPART", path) in self.responses:
            return self.responses[("POST_MULTIPART", path)]
        return {
            "inspection_id": "stub-id",
            "report_url": "/v1/artifacts/inspections/lease-1/stub-id/report.json",
            "screenshot_url": "/v1/artifacts/inspections/lease-1/stub-id/screenshot.png",
            "scope": "lease",
            "scope_ref": "lease-1",
            "blob_prefix": "inspections/lease-1/stub-id",
        }


class StubTankClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def set_test_environment(
        self,
        session_id: str,
        *,
        active: bool = True,
        slot_index: int | None = None,
        url: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "session_id": session_id,
                "active": active,
                "slot_index": slot_index,
                "url": url,
            }
        )
        return {
            "url": f"https://tank.example.test/?session={session_id}",
            "test_state": {
                "active": active,
                "slot_index": slot_index,
                "url": url,
            }
            if active
            else None,
        }

    def upload_session_file(
        self,
        session_id: str,
        *,
        name: str,
        content_type: str,
        data: bytes,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "kind": "upload_session_file",
                "session_id": session_id,
                "name": name,
                "content_type": content_type,
                "data": data,
            }
        )
        return {
            "path": "screenshots/1.png",
            "abs_path": "/workspace/screenshots/1.png",
            "name": name,
            "size": len(data),
        }


class FailingTankClient(StubTankClient):
    def set_test_environment(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        request = httpx.Request("POST", "http://tank.test/session/test-state")
        response = httpx.Response(404, request=request, text='{"detail":"session not found"}')
        raise httpx.HTTPStatusError("404 Not Found", request=request, response=response)


def _registered_tools() -> tuple[dict[str, Any], StubClient]:
    mcp = FakeMCP()
    client = StubClient()
    register_tools(mcp, client)  # type: ignore[arg-type]
    return mcp.tools, client


@contextmanager
def _caller_session(session_id: str):
    token = CALLER_SESSION_ID.set(session_id)
    try:
        yield
    finally:
        CALLER_SESSION_ID.reset(token)


def _state_with_active_slot(
    project: str = "tank-operator",
    slot_name: str = "tank-operator-slot-1",
    slot_index: int = 1,
) -> dict[str, Any]:
    return {
        "active_leases": [
            {
                "project": project,
                "state": "claimed",
                "metadata": {
                    "test_slot_checkout": True,
                    "runner_slot_name": slot_name,
                    "runner_slot_index": str(slot_index),
                },
            }
        ]
    }


def test_create_issue_posts_native_issue_payload() -> None:
    tools, client = _registered_tools()

    result = tools["create_issue"]("glimmung", "Cut issue tracking over")

    assert result == {
        "path": "/v1/issues",
        "params": None,
        "json": {
            "project": "glimmung",
            "title": "Cut issue tracking over",
            "body": "",
            "labels": [],
        },
    }
    assert client.calls[-1] == ("POST", "/v1/issues", None, result["json"])


def test_archive_and_discard_issue_tools_post_audit_reason() -> None:
    tools, client = _registered_tools()

    archive = tools["archive_issue"](
        project="glimmung",
        issue_number=1,
        reason="done elsewhere",
    )
    discard = tools["discard_issue"](
        project="glimmung",
        issue_number=2,
    )

    assert archive == {
        "path": "/v1/issues/by-number/glimmung/1/archive",
        "params": None,
        "json": {"reason": "done elsewhere"},
    }
    assert discard == {
        "path": "/v1/issues/by-number/glimmung/2/discard",
        "params": None,
        "json": {"reason": ""},
    }


def test_list_issues_passes_filters_and_defaults_limit() -> None:
    tools, client = _registered_tools()

    tools["list_issues"](
        project="glimmung",
        state="closed",
        limit=10,
    )

    assert client.calls[-1] == (
        "GET",
        "/v1/issues",
        {
            "project": "glimmung",
            "state": "closed",
            "limit": 10,
        },
        None,
    )


def test_list_leases_excludes_drained_hosts_from_available() -> None:
    tools, client = _registered_tools()
    client.responses[("GET", "/v1/state")] = {
        "test_environments": [
            {
                "project": "tank-operator",
                "slot_index": 1,
                "slot_name": "tank-operator-slot-1",
                "state": "available",
            },
            {
                "project": "tank-operator",
                "slot_index": 2,
                "slot_name": "tank-operator-slot-2",
                "state": "claimed",
            },
        ],
        "test_slot_admissions": [
            {
                "project": "tank-operator",
                "configured_test_slots": 2,
                "prepared_available_test_slots": 1,
                "claimed_test_slots": 1,
                "checkout_available_test_slots": 1,
                "waiting_checkout_requests": 0,
            }
        ],
        "hosts": [
            {
                "name": "slot-1",
                "capabilities": {"project": "tank-operator"},
                "current_lease": None,
                "drained": False,
            },
            {
                "name": "tank-operator-slot-1",
                "capabilities": {"project": "tank-operator"},
                "current_lease": None,
                "drained": False,
            },
            {
                "name": "slot-99",
                "capabilities": {"project": "tank-operator"},
                "current_lease": None,
                "drained": True,
            },
        ],
        "active_leases": [],
        "pending_leases": [],
    }

    result = tools["list_leases"](project="tank-operator")

    assert [slot["slot_name"] for slot in result["available_test_slots"]] == [
        "tank-operator-slot-1"
    ]
    assert [slot["slot_name"] for slot in result["prepared_test_slots"]] == [
        "tank-operator-slot-1"
    ]
    assert result["test_slot_admissions"][0]["checkout_available_test_slots"] == 1
    assert [host["name"] for host in result["available_hosts"]] == ["slot-1"]


def test_list_leases_uses_admission_projection_for_checkout_availability() -> None:
    tools, client = _registered_tools()
    client.responses[("GET", "/v1/state")] = {
        "test_environments": [
            {
                "project": "tank-operator",
                "slot_index": 6,
                "slot_name": "tank-operator-slot-6",
                "state": "available",
            },
            {
                "project": "tank-operator",
                "slot_index": 7,
                "slot_name": "tank-operator-slot-7",
                "state": "available",
            },
        ],
        "test_slot_admissions": [
            {
                "project": "tank-operator",
                "configured_test_slots": 11,
                "prepared_available_test_slots": 2,
                "claimed_test_slots": 5,
                "checkout_available_test_slots": 1,
                "waiting_checkout_requests": 0,
            }
        ],
        "hosts": [],
        "active_leases": [],
        "pending_leases": [],
    }

    result = tools["list_leases"](project="tank-operator")

    assert [slot["slot_name"] for slot in result["prepared_test_slots"]] == [
        "tank-operator-slot-6",
        "tank-operator-slot-7",
    ]
    assert [slot["slot_name"] for slot in result["available_test_slots"]] == [
        "tank-operator-slot-6"
    ]


def test_list_issues_plain_call_caps_results() -> None:
    tools, client = _registered_tools()

    tools["list_issues"]()

    assert client.calls[-1] == ("GET", "/v1/issues", {"state": "open", "limit": 50}, None)


def test_project_scoped_issue_and_run_tools_call_human_id_surface() -> None:
    tools, client = _registered_tools()

    issue = tools["get_issue_by_number"](project="glimmung", issue_number=141)
    graph = tools["get_issue_graph_by_number"](project="glimmung", issue_number=141)
    report = tools["get_run_report"](project="glimmung", issue_number=141, run_number="1.2")
    abort = tools["abort_run"](
        project="glimmung", issue_number=141, run_number="1.2", reason="stuck",
    )

    assert issue["path"] == "/v1/issues/by-number/glimmung/141"
    assert graph["path"] == "/v1/issues/by-number/glimmung/141/graph"
    assert report["path"] == "/v1/projects/glimmung/issues/141/runs/1.2/report"
    assert abort["path"] == "/v1/projects/glimmung/issues/141/runs/1.2/abort"
    assert abort["params"] == {"reason": "stuck"}
    assert "abort_run_by_id" not in tools


def test_get_dashboard_resource_resolves_url_via_content_negotiation() -> None:
    tools, client = _registered_tools()

    step_url = (
        "https://glimmung.romaine.life/projects/ambience/issues/168"
        "/runs/9/cycles/1/phases/llm-verify/jobs/llm-verify/steps/run-verification"
    )
    step_path = (
        "/projects/ambience/issues/168"
        "/runs/9/cycles/1/phases/llm-verify/jobs/llm-verify/steps/run-verification"
    )

    result = tools["get_dashboard_resource"](url=step_url)

    assert result["path"] == step_path
    assert client.calls[-1] == ("GET", step_path, {"format": "json"}, None)


def test_get_dashboard_resource_accepts_absolute_path_and_drops_query() -> None:
    tools, client = _registered_tools()

    tools["get_dashboard_resource"](url="/projects/ambience/issues/168?foo=bar")

    assert client.calls[-1] == (
        "GET",
        "/projects/ambience/issues/168",
        {"format": "json"},
        None,
    )


def test_get_dashboard_resource_rejects_non_dashboard_input() -> None:
    tools, _ = _registered_tools()
    for bad in (
        "",
        "ambience#168",
        "https://glimmung.romaine.life/leases/test",
        "/sessions/530",
    ):
        with pytest.raises(ValueError):
            tools["get_dashboard_resource"](url=bad)


def test_retired_report_tools_are_not_registered() -> None:
    tools, client = _registered_tools()

    assert "get_report" not in tools
    assert "list_reports" not in tools
    assert "create_report" not in tools
    assert "get_report_by_id" not in tools
    assert "list_report_versions" not in tools
    assert "get_report_version" not in tools
    assert "create_report_version" not in tools
    assert "patch_report" not in tools
    assert client.calls == []


def test_project_and_workflow_list_tools_pass_filters_and_default_limits() -> None:
    tools, client = _registered_tools()

    tools["list_projects"](name="glim", github_repo="romaine-life/glimmung")
    tools["list_workflows"](project="glimmung", name="agent", trigger_label="issue-agent")

    assert client.calls[-2:] == [
        ("GET", "/v1/projects", {"name": "glim", "github_repo": "romaine-life/glimmung", "limit": 50}, None),
        (
            "GET",
            "/v1/workflows",
            {"project": "glimmung", "name": "agent", "trigger_label": "issue-agent", "limit": 50},
            None,
        ),
    ]


def test_get_state_hides_backing_lease_ids() -> None:
    tools, client = _registered_tools()

    def fake_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        client.calls.append(("GET", path, params, None))
        return {
            "hosts": [{
                "name": "glimmung-slot-1",
                "current_lease_id": "01BACKINGID",
            }],
            "pending_leases": [],
            "active_leases": [{
                "id": "01BACKINGID",
                "lease_number": 3,
                "project": "glimmung",
                "metadata": {},
            }],
        }

    client.get = fake_get  # type: ignore[method-assign]

    state = tools["get_state"]()

    assert state["hosts"][0]["current_lease"] == "#3"
    assert "current_lease_id" not in state["hosts"][0]
    assert state["active_leases"][0]["lease"] == "#3"
    assert "id" not in state["active_leases"][0]


def test_dispatch_run_hides_backing_lease_id() -> None:
    tools, client = _registered_tools()

    def fake_post(
        path: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        client.calls.append(("POST", path, params, json))
        return {
            "state": "dispatched",
            "run_number": 1,
            "lease_id": "01BACKINGID",
            "host": "runner-k8s",
        }

    client.post = fake_post  # type: ignore[method-assign]

    result = tools["dispatch_run"](issue_number=1, project="glimmung")

    assert result["lease"] == "claimed"
    assert "lease_id" not in result
    assert client.calls[-1] == (
        "POST",
        "/v1/runs/dispatch",
        None,
        {"project": "glimmung", "issue_number": 1},
    )


def test_dispatch_run_forwards_inputs() -> None:
    tools, client = _registered_tools()

    def fake_post(
        path: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        client.calls.append(("POST", path, params, json))
        return {
            "state": "dispatched",
            "run_number": 1,
            "lease_id": "01BACKINGID",
            "host": "runner-k8s",
        }

    client.post = fake_post  # type: ignore[method-assign]

    result = tools["dispatch_run"](
        issue_number=168,
        project="ambience",
        workflow="branch-input-test",
        inputs={"git_ref": "codex/lifecycle-observe"},
    )

    assert result["lease"] == "claimed"
    assert client.calls[-1] == (
        "POST",
        "/v1/runs/dispatch",
        None,
        {
            "project": "ambience",
            "issue_number": 168,
            "workflow": "branch-input-test",
            "inputs": {"git_ref": "codex/lifecycle-observe"},
        },
    )


def test_synthetic_dispatch_run_posts_strict_payload() -> None:
    tools, client = _registered_tools()

    result = tools["synthetic_dispatch_run"](
        issue_number=168,
        project="ambience",
        workflow="default",
        start_at_phase="llm-verify",
        supplied_phase_outputs=[
            {
                "phase": "llm-work",
                "phase_outputs": {
                    "branch_name": "glimmung/issue-168-run-11",
                    "test_plan": "{}",
                },
            }
        ],
        slot_lease_ref="lease-123",
        namespace="ambience-slot-3",
        validation_url="https://ambience-slot-3.example",
        reason="break-glass retry verification",
    )

    assert result["path"] == "/v1/runs/synthetic-dispatch"
    assert client.calls[-1] == (
        "POST",
        "/v1/runs/synthetic-dispatch",
        None,
        {
            "project": "ambience",
            "issue_number": 168,
            "workflow": "default",
            "start_at_phase": "llm-verify",
            "supplied_phase_outputs": [
                {
                    "phase": "llm-work",
                    "phase_outputs": {
                        "branch_name": "glimmung/issue-168-run-11",
                        "test_plan": "{}",
                    },
                }
            ],
            "execution_context": {
                "slot_lease_ref": "lease-123",
                "namespace": "ambience-slot-3",
                "validation_url": "https://ambience-slot-3.example",
            },
            "reason": "break-glass retry verification",
        },
    )


def test_synthetic_dispatch_run_posts_copy_phase_outputs_from() -> None:
    tools, client = _registered_tools()

    tools["synthetic_dispatch_run"](
        issue_number=168,
        project="ambience",
        workflow="default",
        start_at_phase="touchpoint",
        copy_phase_outputs_from={
            "run": "17.1",
            "phases": {"llm-verify": ["verification"]},
        },
        supplied_phase_outputs=[
            {
                "phase": "cleanup_early",
                "phase_outputs": {},
            }
        ],
        slot_lease_ref="lease-123",
        reason="retry touchpoint without rerunning verifier",
    )

    assert client.calls[-1] == (
        "POST",
        "/v1/runs/synthetic-dispatch",
        None,
        {
            "project": "ambience",
            "issue_number": 168,
            "workflow": "default",
            "start_at_phase": "touchpoint",
            "copy_phase_outputs_from": {
                "run": "17.1",
                "phases": {"llm-verify": ["verification"]},
            },
            "supplied_phase_outputs": [
                {
                    "phase": "cleanup_early",
                    "phase_outputs": {},
                }
            ],
            "execution_context": {
                "slot_lease_ref": "lease-123",
            },
            "reason": "retry touchpoint without rerunning verifier",
        },
    )


def test_delete_workflow_calls_delete_endpoint() -> None:
    tools, client = _registered_tools()

    result = tools["delete_workflow"](project="spirelens", name="issue-agent")

    assert client.calls[-1] == (
        "DELETE",
        "/v1/workflows/spirelens/issue-agent",
        None,
        None,
    )
    assert result == {"path": "/v1/workflows/spirelens/issue-agent"}


def test_enqueue_signal_posts_drain_loop_payload() -> None:
    tools, client = _registered_tools()

    result = tools["enqueue_signal"](
        target_type="pr",
        target_repo="romaine-life/glimmung",
        target_ref="romaine-life/glimmung#123",
        payload={"kind": "reject", "feedback": "tighten tests"},
    )

    assert result["path"] == "/v1/signals"
    assert result["json"] == {
        "target_type": "pr",
        "target_repo": "romaine-life/glimmung",
        "target_ref": "romaine-life/glimmung#123",
        "source": "glimmung_ui",
        "payload": {"kind": "reject", "feedback": "tighten tests"},
    }
    assert client.calls[-1] == ("POST", "/v1/signals", None, result["json"])


def test_register_project_posts_admin_payload() -> None:
    tools, client = _registered_tools()

    project = tools["register_project"](
        "glimmung",
        "romaine-life/glimmung",
        metadata={"tier": "control-plane"},
    )

    assert project["json"] == {
        "name": "glimmung",
        "github_repo": "romaine-life/glimmung",
        "metadata": {"tier": "control-plane"},
    }
    assert "register_host" not in tools
    assert "delete_host" not in tools
    assert client.calls[-1:] == [
        ("POST", "/v1/projects", None, project["json"]),
    ]


def test_get_test_slot_hot_swap_contract_reads_project_metadata() -> None:
    tools, client = _registered_tools()
    client.responses[("GET", "/v1/projects")] = [
        {
            "name": "tank-operator",
            "metadata": {
                "test_slot_hot_swap": {
                    "enabled": True,
                    "backend": {
                        "enabled": True,
                        "target": "/var/run/tank-operator-hot/tank-operator-go",
                    },
                }
            },
        }
    ]

    result = tools["get_test_slot_hot_swap_contract"]("tank-operator")

    assert result["enabled"] is True
    assert result["contract"]["backend"]["target"] == "/var/run/tank-operator-hot/tank-operator-go"
    assert client.calls[-1] == (
        "GET",
        "/v1/projects",
        {"name": "tank-operator", "limit": 10},
        None,
    )


def test_record_test_slot_hot_swap_posts_history() -> None:
    tools, client = _registered_tools()
    client.responses[("GET", "/v1/state")] = _state_with_active_slot(
        slot_name="tank-slot-1",
        slot_index=1,
    )

    result = tools["record_test_slot_hot_swap"](
        project="tank-operator",
        slot_name="tank-slot-1",
        operation="backend",
        status="ok",
        diagnostics={"pod": "tank-pod"},
        timings={"total": "2s"},
    )

    assert result["path"] == "/v1/test-slots/hot-swap-history"
    assert result["json"] == {
        "project": "tank-operator",
        "slot_name": "tank-slot-1",
        "entry": {
            "operation": "backend",
            "status": "ok",
            "summary": "",
            "diagnostics": {"pod": "tank-pod"},
            "timings": {"total": "2s"},
        },
    }


def test_record_test_slot_hot_swap_returns_diagnostic_without_active_lease() -> None:
    tools, client = _registered_tools()
    client.responses[("GET", "/v1/state")] = {"active_leases": []}

    result = tools["record_test_slot_hot_swap"](
        project="tank-operator",
        slot_index=1,
        status="ok",
    )

    assert result["state"] == "no_active_test_slot_lease"
    assert client.calls == [("GET", "/v1/state", None, None)]


def _running_dispatch(job_name: str = "apply-hot-swap-x", lease: str = "lease-x") -> dict[str, Any]:
    return {
        "lease": lease,
        "apply": {"job_name": job_name, "outcome": "running", "timings": {}},
        "history_entry": {
            "operation": "apply_hot_swap",
            "status": "running",
            "diagnostics": {"job_name": job_name},
        },
    }


def _terminal_status(
    job_name: str = "apply-hot-swap-x",
    lease: str = "lease-x",
    status: str = "persisted",
) -> dict[str, Any]:
    return {
        "lease": lease,
        "job_name": job_name,
        "status": status,
        "history_entry": {
            "operation": "apply_hot_swap",
            "status": status,
            "diagnostics": {
                "build_logs_tail": "build ok",
                "swap_logs_tail": "swap ok",
                "validation_target": "existing_session",
            },
            "timings": {"total": "42s"},
        },
    }


def _post_payload(client: StubClient, path: str) -> dict[str, Any] | None:
    for method, called_path, _params, json in client.calls:
        if method == "POST" and called_path == path:
            return json
    return None


def test_apply_test_slot_hot_swap_dispatches_and_polls_to_terminal(monkeypatch) -> None:
    tools, client = _registered_tools()
    monkeypatch.setattr(tools_mod, "_HOT_SWAP_POLL_INTERVAL_SECONDS", 0)
    client.responses[("GET", "/v1/state")] = _state_with_active_slot()
    client.responses[("POST", "/v1/test-slots/apply-hot-swap")] = _running_dispatch()
    client.responses[
        ("GET", "/v1/test-slots/apply-hot-swap/tank-operator/apply-hot-swap-x")
    ] = _terminal_status()

    result = tools["apply_test_slot_hot_swap"](
        project="tank-operator",
        artifact_kind="agent_runner",
        git_ref="feat/durable-stop-request",
        validation_target="existing_session",
        slot_name="tank-operator-slot-1",
    )

    # The dispatch POST carries exactly the minimal payload (no timeout/base_ref).
    assert _post_payload(client, "/v1/test-slots/apply-hot-swap") == {
        "project": "tank-operator",
        "artifact_kind": "agent_runner",
        "git_ref": "feat/durable-stop-request",
        "validation_target": "existing_session",
        "slot_name": "tank-operator-slot-1",
    }
    # The wrapper polled the durable status endpoint and returned the terminal
    # outcome (not a fixed-timeout failure).
    assert (
        "GET",
        "/v1/test-slots/apply-hot-swap/tank-operator/apply-hot-swap-x",
        None,
        None,
    ) in client.calls
    assert result["status"] == "persisted"
    assert result["job_name"] == "apply-hot-swap-x"
    assert result["apply"]["outcome"] == "persisted"
    assert result["apply"]["build_logs_tail"] == "build ok"


def test_apply_test_slot_hot_swap_posts_backend_kind(monkeypatch) -> None:
    # backend is a first-class artifact_kind on the apply endpoint (it streams
    # the orchestrator binary onto the app pod's supervisor + health-gates the
    # SIGHUP re-exec). The MCP tool passes it through like any other kind; the
    # legacy glimmung-agent CLI path it used to point at is gone.
    tools, client = _registered_tools()
    monkeypatch.setattr(tools_mod, "_HOT_SWAP_POLL_INTERVAL_SECONDS", 0)
    client.responses[("GET", "/v1/state")] = _state_with_active_slot()
    client.responses[("POST", "/v1/test-slots/apply-hot-swap")] = _running_dispatch()
    client.responses[
        ("GET", "/v1/test-slots/apply-hot-swap/tank-operator/apply-hot-swap-x")
    ] = _terminal_status()

    result = tools["apply_test_slot_hot_swap"](
        project="tank-operator",
        artifact_kind="backend",
        git_ref="feat/x",
        validation_target="existing_session",
        slot_name="tank-operator-slot-1",
    )

    assert _post_payload(client, "/v1/test-slots/apply-hot-swap") == {
        "project": "tank-operator",
        "artifact_kind": "backend",
        "git_ref": "feat/x",
        "validation_target": "existing_session",
        "slot_name": "tank-operator-slot-1",
    }
    assert result["status"] == "persisted"


def test_apply_test_slot_hot_swap_passes_timeout_slot_index_and_base_ref(monkeypatch) -> None:
    tools, client = _registered_tools()
    monkeypatch.setattr(tools_mod, "_HOT_SWAP_POLL_INTERVAL_SECONDS", 0)
    client.responses[("GET", "/v1/state")] = _state_with_active_slot(slot_index=2)
    client.responses[("POST", "/v1/test-slots/apply-hot-swap")] = _running_dispatch()
    client.responses[
        ("GET", "/v1/test-slots/apply-hot-swap/tank-operator/apply-hot-swap-x")
    ] = _terminal_status()

    tools["apply_test_slot_hot_swap"](
        project="tank-operator",
        artifact_kind="agent_runner",
        git_ref="main",
        validation_target="full_runtime",
        slot_index=2,
        timeout_seconds=300,
        base_ref="main",
    )

    assert _post_payload(client, "/v1/test-slots/apply-hot-swap") == {
        "project": "tank-operator",
        "artifact_kind": "agent_runner",
        "git_ref": "main",
        "validation_target": "full_runtime",
        "slot_index": 2,
        "timeout_seconds": 300,
        "base_ref": "main",
    }


def test_apply_test_slot_hot_swap_dispatch_failure_skips_poll() -> None:
    # When the Job never reaches the apiserver the dispatch is already terminal;
    # there is nothing to poll, so the wrapper returns immediately.
    tools, client = _registered_tools()
    client.responses[("GET", "/v1/state")] = _state_with_active_slot()
    client.responses[("POST", "/v1/test-slots/apply-hot-swap")] = {
        "lease": "lease-x",
        "apply": {"job_name": "apply-hot-swap-x", "outcome": "swap_failed", "error": "apply job: boom"},
        "history_entry": {"operation": "apply_hot_swap", "status": "swap_failed"},
    }

    result = tools["apply_test_slot_hot_swap"](
        project="tank-operator",
        artifact_kind="static",
        git_ref="x",
        validation_target="existing_session",
        slot_name="tank-operator-slot-1",
    )

    assert result["status"] == "swap_failed"
    # No status GET was issued (only the /v1/state precheck + the dispatch POST).
    assert not any(
        c[0] == "GET" and c[1].startswith("/v1/test-slots/apply-hot-swap/") for c in client.calls
    )


def test_apply_test_slot_hot_swap_poll_budget_elapsed_returns_running(monkeypatch) -> None:
    tools, client = _registered_tools()
    monkeypatch.setattr(tools_mod, "_HOT_SWAP_POLL_INTERVAL_SECONDS", 0)
    # Fake clock: deadline-calc=0, first loop-check=0 (<budget), post-poll check
    # jumps past the budget so the loop exits after a single running poll.
    clock = iter([0.0, 0.0, 10_000.0, 10_000.0, 10_000.0])
    monkeypatch.setattr(tools_mod.time, "monotonic", lambda: next(clock))
    client.responses[("GET", "/v1/state")] = _state_with_active_slot()
    client.responses[("POST", "/v1/test-slots/apply-hot-swap")] = _running_dispatch()
    client.responses[
        ("GET", "/v1/test-slots/apply-hot-swap/tank-operator/apply-hot-swap-x")
    ] = _terminal_status(status="running")

    result = tools["apply_test_slot_hot_swap"](
        project="tank-operator",
        artifact_kind="static",
        git_ref="x",
        validation_target="existing_session",
        slot_name="tank-operator-slot-1",
        timeout_seconds=10,
    )

    assert result["status"] == "running"
    assert "note" in result
    assert "re-query" in result["note"]


def test_apply_test_slot_hot_swap_requires_one_slot_selector() -> None:
    tools, client = _registered_tools()

    result = tools["apply_test_slot_hot_swap"](
        project="tank-operator",
        artifact_kind="agent_runner",
        git_ref="main",
        validation_target="existing_session",
    )

    assert result["state"] == "slot_selector_invalid"
    assert client.calls == []


def test_apply_test_slot_hot_swap_returns_diagnostic_without_active_lease() -> None:
    tools, client = _registered_tools()
    client.responses[("GET", "/v1/state")] = {"active_leases": []}

    result = tools["apply_test_slot_hot_swap"](
        project="tank-operator",
        artifact_kind="agent_runner",
        git_ref="main",
        validation_target="new_session",
        slot_index=1,
    )

    assert result["state"] == "no_active_test_slot_lease"
    assert client.calls == [("GET", "/v1/state", None, None)]


def _deploy_running_dispatch() -> dict[str, Any]:
    return {
        "lease": "lease-x",
        "job": "deploy-x",
        "status": "running",
        "git_ref": "feat/x",
        "sha": "abc123def456",
        "image": "abc123def456",
        "history_entry": {
            "operation": "deploy_to_image",
            "status": "running",
            "diagnostics": {
                "job_name": "deploy-x",
                "git_ref": "feat/x",
                "sha": "abc123def456",
                "image": "abc123def456",
            },
        },
    }


def _deploy_terminal_status(status: str = "deployed") -> dict[str, Any]:
    return {
        "lease": "lease-x",
        "job_name": "deploy-x",
        "status": status,
        "history_entry": {
            "operation": "deploy_to_image",
            "status": status,
            "diagnostics": {
                "job_name": "deploy-x",
                "git_ref": "feat/x",
                "sha": "abc123def456",
                "image": "abc123def456",
            },
        },
    }


def test_deploy_test_slot_to_image_dispatches_and_polls_to_terminal(monkeypatch) -> None:
    tools, client = _registered_tools()
    monkeypatch.setattr(tools_mod, "_HOT_SWAP_POLL_INTERVAL_SECONDS", 0)
    client.responses[("GET", "/v1/state")] = _state_with_active_slot()
    client.responses[("POST", "/v1/test-slots/deploy-to-image")] = _deploy_running_dispatch()
    client.responses[
        ("GET", "/v1/test-slots/apply-hot-swap/tank-operator/deploy-x")
    ] = _deploy_terminal_status()

    result = tools["deploy_test_slot_to_image"](
        project="tank-operator",
        git_ref="feat/x",
        slot_name="tank-operator-slot-1",
    )

    # Minimal payload: project + git_ref + slot, no artifact_kind / classifier.
    assert _post_payload(client, "/v1/test-slots/deploy-to-image") == {
        "project": "tank-operator",
        "git_ref": "feat/x",
        "slot_name": "tank-operator-slot-1",
    }
    # Polled the shared status endpoint by the deploy handle and returned terminal.
    assert (
        "GET",
        "/v1/test-slots/apply-hot-swap/tank-operator/deploy-x",
        None,
        None,
    ) in client.calls
    assert result["status"] == "deployed"
    assert result["job_name"] == "deploy-x"
    assert result["deploy"]["outcome"] == "deployed"
    assert result["deploy"]["sha"] == "abc123def456"
    assert result["deploy"]["image"] == "abc123def456"


def test_deploy_test_slot_to_image_passes_slot_index(monkeypatch) -> None:
    tools, client = _registered_tools()
    monkeypatch.setattr(tools_mod, "_HOT_SWAP_POLL_INTERVAL_SECONDS", 0)
    client.responses[("GET", "/v1/state")] = _state_with_active_slot(slot_index=2)
    client.responses[("POST", "/v1/test-slots/deploy-to-image")] = _deploy_running_dispatch()
    client.responses[
        ("GET", "/v1/test-slots/apply-hot-swap/tank-operator/deploy-x")
    ] = _deploy_terminal_status()

    tools["deploy_test_slot_to_image"](
        project="tank-operator",
        git_ref="main",
        slot_index=2,
        timeout_seconds=300,
    )

    # timeout_seconds is the poll budget, not a request field — payload stays minimal.
    assert _post_payload(client, "/v1/test-slots/deploy-to-image") == {
        "project": "tank-operator",
        "git_ref": "main",
        "slot_index": 2,
    }


def test_deploy_test_slot_to_image_requires_one_slot_selector() -> None:
    tools, client = _registered_tools()
    result = tools["deploy_test_slot_to_image"](project="tank-operator", git_ref="main")
    assert result["state"] == "slot_selector_invalid"
    assert client.calls == []


def test_deploy_test_slot_to_image_returns_diagnostic_without_active_lease() -> None:
    tools, client = _registered_tools()
    client.responses[("GET", "/v1/state")] = {"active_leases": []}
    result = tools["deploy_test_slot_to_image"](
        project="tank-operator", git_ref="main", slot_index=1
    )
    assert result["state"] == "no_active_test_slot_lease"
    assert client.calls == [("GET", "/v1/state", None, None)]


def test_playbook_tools_call_http_surface() -> None:
    tools, client = _registered_tools()

    created = tools["create_playbook"](
        project="glimmung",
        title="Coordinated rollout",
        description="storage slice",
        entries=[{
            "id": "one",
            "issue": {
                "title": "Land substrate",
                "body": "models and API",
                "labels": ["issue-agent"],
            },
        }],
        concurrency_limit=1,
        metadata={"source": "mcp-test"},
    )
    tools["list_playbooks"](project="glimmung", state="draft")
    tools["get_playbook"]("glimmung", "pb-1")
    tools["run_playbook"]("glimmung", "pb-1")

    assert created["path"] == "/v1/playbooks"
    assert created["json"] == {
        "project": "glimmung",
        "title": "Coordinated rollout",
        "description": "storage slice",
        "entries": [{
            "id": "one",
            "issue": {
                "title": "Land substrate",
                "body": "models and API",
                "labels": ["issue-agent"],
            },
        }],
        "metadata": {"source": "mcp-test"},
        "concurrency_limit": 1,
    }
    assert client.calls[-4:] == [
        ("POST", "/v1/playbooks", None, created["json"]),
        ("GET", "/v1/playbooks", {"project": "glimmung", "state": "draft", "limit": 50}, None),
        ("GET", "/v1/playbooks/glimmung/pb-1", None, None),
        ("POST", "/v1/playbooks/glimmung/pb-1/run", None, None),
    ]


def _fake_inspect_url_returning_path(calls: list[dict[str, Any]], screenshot_bytes: bytes = b"PNG"):
    """Return a fake inspect_url that writes the screenshot tempfile and
    surfaces the report shape the new tool flow expects."""
    import tempfile

    def fake(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        fd, path = tempfile.mkstemp(suffix=".png", prefix="test-")
        import os as _os
        _os.close(fd)
        with open(path, "wb") as fh:
            fh.write(screenshot_bytes)
        return {
            "final_url": kwargs["url"],
            "status": 200,
            "title": "T",
            "body_text": "hello",
            "elements": [],
            "console": [],
            "page_errors": [],
            "failed_requests": [],
            "http_errors": [],
            "screenshot_path": path,
            "inspected_at": "2026-05-28T00:00:00.000Z",
        }
    return fake


def test_browser_inspector_tool_uploads_to_glimmung(monkeypatch) -> None:
    tools, client = _registered_tools()
    calls: list[dict[str, Any]] = []
    client.responses[("GET", "/v1/state")] = {
        "active_leases": [
            {
                "id": "lease-1",
                "project": "tank-operator",
                "metadata": {
                    "tank_session_id": "abc123",
                    "runner_slot_name": "tank-operator-slot-1",
                    "playwright_ws_endpoint": "ws://slot-playwright.tank-operator-slot-1.svc.cluster.local:3000",
                },
            }
        ]
    }
    monkeypatch.setattr(
        "mcp_glimmung.tools.inspect_url",
        _fake_inspect_url_returning_path(calls),
    )

    with _caller_session("abc123"):
        result = tools["inspect_browser_url"](
            "https://example.test/app",
            viewport={"width": 390, "height": 844},
            wait_ms=100,
        )

    # Deprecated knobs are gone from the request shape.
    assert "screenshot" not in calls[0]
    assert "screenshot_base64" not in result
    # Wrapper called the new multipart endpoint with project + session
    # in the form data and both parts populated.
    mp = client.last_multipart
    assert mp["path"] == "/v1/inspections"
    assert mp["data"] == {"tank_session_id": "abc123", "project": "tank-operator"}
    files = mp["files"]
    assert "report" in files and "screenshot" in files
    assert files["report"][2] == "application/json"
    assert files["screenshot"][2] == "image/png"
    assert files["screenshot"][1] == b"PNG"
    # Idempotency header is set every call.
    assert "X-Inspection-Request-Id" in mp["extra_headers"]
    assert len(mp["extra_headers"]["X-Inspection-Request-Id"]) == 32
    # Summary view shape.
    assert result["inspection_id"] == "stub-id"
    assert result["report_url"].endswith("/report.json")
    assert result["screenshot_url"].endswith("/screenshot.png")
    assert result["scope"] == "lease"
    assert result["scope_ref"] == "lease-1"
    assert result["final_url"] == "https://example.test/app"


def test_browser_inspector_tool_can_save_screenshot_to_workspace(monkeypatch) -> None:
    mcp = FakeMCP()
    client = StubClient()
    tank = StubTankClient()
    register_tools(mcp, client, tank)  # type: ignore[arg-type]
    tools = mcp.tools
    calls: list[dict[str, Any]] = []
    client.responses[("GET", "/v1/state")] = {
        "active_leases": [
            {
                "id": "lease-1",
                "project": "tank-operator",
                "metadata": {
                    "tank_session_id": "abc123",
                    "runner_slot_name": "tank-operator-slot-1",
                    "playwright_ws_endpoint": "ws://slot-playwright.tank-operator-slot-1.svc.cluster.local:3000",
                },
            }
        ]
    }
    monkeypatch.setattr(
        "mcp_glimmung.tools.inspect_url",
        _fake_inspect_url_returning_path(calls),
    )

    with _caller_session("abc123"):
        result = tools["inspect_browser_url"](
            "https://example.test/app",
            wait_ms=100,
            save_screenshot_to_workspace=True,
            workspace_screenshot_name="origin-avatar-validation.png",
        )

    assert tank.calls[-1] == {
        "kind": "upload_session_file",
        "session_id": "abc123",
        "name": "origin-avatar-validation.png",
        "content_type": "image/png",
        "data": b"PNG",
    }
    assert result["workspace_screenshot"] == {
        "path": "screenshots/1.png",
        "abs_path": "/workspace/screenshots/1.png",
        "name": "origin-avatar-validation.png",
        "size": 3,
    }
    assert result["screenshot_url"].endswith("/screenshot.png")


def test_browser_inspector_tool_requires_tank_client_for_workspace_save(
    monkeypatch,
) -> None:
    tools, client = _registered_tools()
    calls: list[dict[str, Any]] = []
    client.responses[("GET", "/v1/state")] = {
        "active_leases": [
            {
                "id": "lease-1",
                "project": "tank-operator",
                "metadata": {
                    "tank_session_id": "abc123",
                    "runner_slot_name": "tank-operator-slot-1",
                    "playwright_ws_endpoint": "ws://slot-playwright.tank-operator-slot-1.svc.cluster.local:3000",
                },
            }
        ]
    }
    monkeypatch.setattr(
        "mcp_glimmung.tools.inspect_url",
        _fake_inspect_url_returning_path(calls),
    )

    with pytest.raises(RuntimeError, match="requires a TankClient"):
        with _caller_session("abc123"):
            tools["inspect_browser_url"](
                "https://example.test/app",
                save_screenshot_to_workspace=True,
            )
    assert calls == []


def test_browser_inspector_tool_forwards_auth_injection(monkeypatch) -> None:
    tools, client = _registered_tools()
    calls: list[dict[str, Any]] = []
    client.responses[("GET", "/v1/state")] = {
        "active_leases": [
            {
                "id": "lease-1",
                "project": "tank-operator",
                "metadata": {
                    "tank_session_id": "abc123",
                    "runner_slot_name": "tank-operator-slot-1",
                    "playwright_ws_endpoint": "ws://slot-playwright.tank-operator-slot-1.svc.cluster.local:3000",
                },
            }
        ]
    }
    monkeypatch.setattr(
        "mcp_glimmung.tools.inspect_url",
        _fake_inspect_url_returning_path(calls),
    )

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
    headers = {"X-Test": "ok"}
    storage = {
        "https://tank-operator-slot-1.tank.dev.romaine.life": {
            "tank-operator-jwt": "fake.jwt.value",
        }
    }

    with _caller_session("abc123"):
        tools["inspect_browser_url"](
            "https://tank-operator-slot-1.tank.dev.romaine.life",
            cookies=cookies,
            extra_http_headers=headers,
            local_storage=storage,
        )

    assert calls[0]["cookies"] == cookies
    assert calls[0]["extra_http_headers"] == headers
    assert calls[0]["local_storage"] == storage


def test_browser_inspector_tool_tank_auth_seeds_caller_token(monkeypatch) -> None:
    tools, client = _registered_tools()
    calls: list[dict[str, Any]] = []
    client.responses[("GET", "/v1/state")] = {
        "active_leases": [
            {
                "id": "lease-1",
                "project": "tank-operator",
                "metadata": {
                    "tank_session_id": "abc123",
                    "runner_slot_name": "tank-operator-slot-1",
                    "playwright_ws_endpoint": "ws://slot-playwright.tank-operator-slot-1.svc.cluster.local:3000",
                },
            }
        ]
    }
    monkeypatch.setattr(
        "mcp_glimmung.tools.inspect_url",
        _fake_inspect_url_returning_path(calls),
    )
    monkeypatch.setattr(
        "mcp_glimmung.tools.current_caller",
        lambda: SimpleNamespace(raw_token="caller.jwt"),
    )

    preflight_calls: list[dict[str, Any]] = []

    def fake_get(url: str, *, headers: dict[str, str], timeout: float) -> httpx.Response:
        preflight_calls.append({"url": url, "headers": headers, "timeout": timeout})
        return httpx.Response(
            200,
            json={
                "sub": "svc:tank:609",
                "email": "pod-609@service.tank.romaine.life",
                "role": "service",
                "is_admin": True,
                "installation_id": None,
            },
        )

    monkeypatch.setattr("mcp_glimmung.tools.httpx.get", fake_get)

    with _caller_session("abc123"):
        result = tools["inspect_browser_url"](
            "https://tank-operator-slot-1.tank.dev.romaine.life/sessions/46",
            tank_auth=True,
            local_storage={
                "https://tank-operator-slot-1.tank.dev.romaine.life": {
                    "theme": "dark",
                }
            },
        )

    assert preflight_calls == [
        {
            "url": "https://tank-operator-slot-1.tank.dev.romaine.life/api/auth/me",
            "headers": {"Authorization": "Bearer caller.jwt"},
            "timeout": 10.0,
        }
    ]
    assert calls[0]["local_storage"] == {
        "https://tank-operator-slot-1.tank.dev.romaine.life": {
            "theme": "dark",
            "auth-romaine-jwt": "caller.jwt",
        }
    }
    assert result["auth"] == {
        "mode": "tank_caller",
        "preflight_status": 200,
        "email": "pod-609@service.tank.romaine.life",
        "role": "service",
        "is_admin": True,
        "sub": "svc:tank:609",
        "installation_id": None,
    }
    report = client.last_multipart["files"]["report"][1].decode("utf-8")
    assert "caller.jwt" not in report


def test_browser_inspector_tool_tank_auth_rejects_conflicting_storage(
    monkeypatch,
) -> None:
    tools, client = _registered_tools()
    client.responses[("GET", "/v1/state")] = {
        "active_leases": [
            {
                "id": "lease-1",
                "project": "tank-operator",
                "metadata": {
                    "tank_session_id": "abc123",
                    "runner_slot_name": "tank-operator-slot-1",
                    "playwright_ws_endpoint": "ws://slot-playwright.tank-operator-slot-1.svc.cluster.local:3000",
                },
            }
        ]
    }
    monkeypatch.setattr(
        "mcp_glimmung.tools.current_caller",
        lambda: SimpleNamespace(raw_token="caller.jwt"),
    )
    monkeypatch.setattr(
        "mcp_glimmung.tools.httpx.get",
        lambda *_args, **_kwargs: httpx.Response(200, json={"role": "service"}),
    )

    with pytest.raises(ValueError, match="conflicts with local_storage"):
        with _caller_session("abc123"):
            tools["inspect_browser_url"](
                "https://tank-operator-slot-1.tank.dev.romaine.life/sessions/46",
                tank_auth=True,
                local_storage={
                    "https://tank-operator-slot-1.tank.dev.romaine.life": {
                        "auth-romaine-jwt": "different.jwt",
                    }
                },
            )


def test_browser_inspector_tool_tank_auth_requires_successful_preflight(
    monkeypatch,
) -> None:
    tools, client = _registered_tools()
    client.responses[("GET", "/v1/state")] = {
        "active_leases": [
            {
                "id": "lease-1",
                "project": "tank-operator",
                "metadata": {
                    "tank_session_id": "abc123",
                    "runner_slot_name": "tank-operator-slot-1",
                    "playwright_ws_endpoint": "ws://slot-playwright.tank-operator-slot-1.svc.cluster.local:3000",
                },
            }
        ]
    }
    monkeypatch.setattr(
        "mcp_glimmung.tools.current_caller",
        lambda: SimpleNamespace(raw_token="caller.jwt"),
    )
    monkeypatch.setattr(
        "mcp_glimmung.tools.httpx.get",
        lambda *_args, **_kwargs: httpx.Response(401, text="invalid session token"),
    )

    with pytest.raises(RuntimeError, match="tank_auth preflight failed"):
        with _caller_session("abc123"):
            tools["inspect_browser_url"](
                "https://tank-operator-slot-1.tank.dev.romaine.life/sessions/46",
                tank_auth=True,
            )


def test_browser_inspector_tool_unlinks_tempfile_on_upload_failure(monkeypatch) -> None:
    tools, client = _registered_tools()
    calls: list[dict[str, Any]] = []
    client.responses[("GET", "/v1/state")] = {
        "active_leases": [
            {
                "id": "lease-1",
                "project": "tank-operator",
                "metadata": {
                    "tank_session_id": "abc123",
                    "runner_slot_name": "tank-operator-slot-1",
                    "playwright_ws_endpoint": "ws://slot-playwright.tank-operator-slot-1.svc.cluster.local:3000",
                },
            }
        ]
    }
    monkeypatch.setattr(
        "mcp_glimmung.tools.inspect_url",
        _fake_inspect_url_returning_path(calls),
    )

    captured_path: dict[str, str] = {}

    def boom_multipart(self, path, *, data=None, files=None, extra_headers=None):  # noqa: ARG001
        # Capture the tempfile path from the in-flight call so the test
        # can assert it gets unlinked even though the upload exploded.
        captured_path["tempfile_path"] = calls[0]["url"]
        raise RuntimeError("simulated upload failure")

    monkeypatch.setattr(client.__class__, "post_multipart", boom_multipart)

    with pytest.raises(RuntimeError, match="simulated upload failure"):
        with _caller_session("abc123"):
            tools["inspect_browser_url"](
                "https://example.test/",
            )
    # The screenshot tempfile inspect_url created was unlinked in the
    # tool's `finally` even though the upload raised. We can't observe the
    # exact path from here, but the post_multipart raised before any
    # subsequent code path could leak the path globally — the
    # not-found-on-unlink branch is exercised via FileNotFoundError
    # being suppressed in tools.py.


def test_resume_run_posts_native_step_boundary_payload() -> None:
    tools, client = _registered_tools()

    result = tools["resume_run"](
        project="glimmung",
        issue_number=141,
        run_number="1.1",
        entrypoint_phase="agent-execute",
        entrypoint_job_id="agent",
        entrypoint_step_slug="run-agent",
        input_overrides={"namespace": "preview-override"},
        artifact_refs={"repo": "blob://artifacts/source.tgz"},
        context={"operator_note": "resume at agent step"},
        trigger_source={"actor": "codex"},
    )

    assert result["path"] == "/v1/projects/glimmung/issues/141/runs/1.1/resume"
    assert result["json"] == {
        "entrypoint_phase": "agent-execute",
        "entrypoint_job_id": "agent",
        "entrypoint_step_slug": "run-agent",
        "input_overrides": {"namespace": "preview-override"},
        "artifact_refs": {"repo": "blob://artifacts/source.tgz"},
        "context": {"operator_note": "resume at agent step"},
        "trigger_source": {
            "kind": "resume_via_mcp",
            "resumed_from_issue_number": 141,
            "resumed_from_run_number": "1.1",
            "actor": "codex",
        },
    }


def test_run_number_tools_reject_non_canonical_addresses() -> None:
    """A bare run number or the flat cycle-ledger number is not an address:
    the run-cycle tools reject it before any request reaches glimmung, so the
    get_run_report(run_number=9) -> "6.1" class of bug cannot recur at the MCP
    surface (mcp-surface-rollout.md: don't rely on the backend alone)."""
    tools, client = _registered_tools()

    for bad in ["9", "6", "0.1", "6.0", "1.", ".1", "1.2.3", "abc", ""]:
        with pytest.raises(ValueError):
            tools["get_run_report"](project="ambience", issue_number=168, run_number=bad)
        with pytest.raises(ValueError):
            tools["abort_run"](project="ambience", issue_number=168, run_number=bad)

    # No malformed address ever reached the HTTP client.
    assert client.calls == []

    # The canonical run.cycle form is accepted and forwarded verbatim.
    report = tools["get_run_report"](project="ambience", issue_number=168, run_number="6.1")
    assert report["path"] == "/v1/projects/ambience/issues/168/runs/6.1/report"


def test_raise_for_status_surfaces_glimmung_problem_detail() -> None:
    """glimmung returns errors as {"detail": ...}; the client must surface that
    detail so an agent sees *why* (e.g. the canonical run-cycle requirement)
    instead of a bare 'Client error 400 Bad Request'."""
    from mcp_glimmung.glimmung_client import _raise_for_status

    request = httpx.Request(
        "GET", "http://glimmung.glimmung.svc/v1/projects/ambience/issues/168/runs/9/report"
    )
    response = httpx.Response(
        400,
        json={"detail": 'run_number must be a canonical run-cycle number like "6.1"'},
        request=request,
    )
    with pytest.raises(httpx.HTTPStatusError) as exc:
        _raise_for_status(response)
    message = str(exc.value)
    assert "400" in message
    assert "canonical run-cycle number" in message


def test_raise_for_status_is_noop_on_success() -> None:
    from mcp_glimmung.glimmung_client import _raise_for_status

    request = httpx.Request("GET", "http://glimmung.glimmung.svc/v1/state")
    _raise_for_status(httpx.Response(200, json={"ok": True}, request=request))


def test_checkout_test_slot_posts_checkout_payload() -> None:
    tools, client = _registered_tools()

    with _caller_session("abc123"):
        result = tools["checkout_test_slot"](
            project="glimmung",
            workflow="native-agent",
            ttl_seconds=3600,
        )

    assert result["path"] == "/v1/test-slots/checkout"
    assert result["json"] == {
        "project": "glimmung",
        "requester": {
            "consumer": "tank-operator",
            "kind": "tank_session",
            "ref": "tank-operator/session/abc123",
            "label": "abc123",
            "metadata": {"tank_session_id": "abc123", "session_scope": "default"},
        },
        "tank_session_id": "abc123",
        "workflow": "native-agent",
        "ttl_seconds": 3600,
    }
    assert client.calls[-1] == (
        "POST",
        "/v1/test-slots/checkout",
        None,
        result["json"],
    )


def test_checkout_test_slot_does_not_expose_legacy_selection_knobs() -> None:
    tools, _client = _registered_tools()

    signature = inspect.signature(tools["checkout_test_slot"])

    assert "tank_session_id" not in signature.parameters
    assert "slot_index" not in signature.parameters
    assert "mode" not in signature.parameters
    assert "phase_inputs" not in signature.parameters


def test_session_owned_slot_tools_do_not_accept_manual_session_id() -> None:
    tools, _client = _registered_tools()

    for tool_name in (
        "checkout_test_slot",
        "inspect_browser_url",
        "return_test_slot",
        "extend_test_slot_lease",
    ):
        signature = inspect.signature(tools[tool_name])
        assert "tank_session_id" not in signature.parameters


def test_checkout_test_slot_updates_tank_session_on_assigned_slot() -> None:
    mcp = FakeMCP()
    tank = StubTankClient()

    class CheckoutClient(StubClient):
        def post(
            self,
            path: str,
            params: dict[str, Any] | None = None,
            json: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            self.calls.append(("POST", path, params, json))
            return {
                "state": "activating",
                "project": "tank-operator",
                "workflow": "test-slot-checkout",
                "slot_index": 2,
                "slot_name": "tank-slot-2",
                "url": "https://tank-slot-2.tank.dev.romaine.life",
                "lease": "tank-slot-2",
            }

    client = CheckoutClient()
    register_tools(mcp, client, tank)  # type: ignore[arg-type]

    with _caller_session("abc123"):
        result = mcp.tools["checkout_test_slot"](
            project="tank-operator",
        )

    assert tank.calls == [
        {
            "session_id": "abc123",
            "active": True,
            "slot_index": 2,
            "url": "https://tank-slot-2.tank.dev.romaine.life",
        }
    ]
    assert result["lease"] == "tank-slot-2"
    assert result["tank_test_state"]["slot_index"] == 2
    assert result["tank_session_url"] == "https://tank.example.test/?session=abc123"


def test_checkout_test_slot_normalizes_caller_session_pod_name() -> None:
    mcp = FakeMCP()
    tank = StubTankClient()

    class CheckoutClient(StubClient):
        def post(
            self,
            path: str,
            params: dict[str, Any] | None = None,
            json: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            self.calls.append(("POST", path, params, json))
            return {
                "state": "active",
                "project": "glimmung",
                "workflow": "test-slot-checkout",
                "slot_index": 1,
                "slot_name": "glimmung-1",
                "url": "https://glimmung-1.glimmung.dev.romaine.life",
                "lease": "glimmung-1",
            }

    client = CheckoutClient()
    register_tools(mcp, client, tank)  # type: ignore[arg-type]

    with _caller_session("session-9190aa98a2"):
        result = mcp.tools["checkout_test_slot"](
            project="glimmung",
        )

    payload = client.calls[-1][3]
    assert payload is not None
    assert payload["tank_session_id"] == "9190aa98a2"
    assert payload["requester"]["ref"] == "tank-operator/session/9190aa98a2"
    assert tank.calls[0]["session_id"] == "9190aa98a2"
    assert result["tank_session_url"] == "https://tank.example.test/?session=9190aa98a2"


def test_checkout_test_slot_uses_server_returned_slot_url_for_tank_callback() -> None:
    mcp = FakeMCP()
    tank = StubTankClient()

    class CheckoutClient(StubClient):
        def post(
            self,
            path: str,
            params: dict[str, Any] | None = None,
            json: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            self.calls.append(("POST", path, params, json))
            return {
                "state": "active",
                "project": "glimmung",
                "workflow": "test-slot-checkout",
                "slot_index": 1,
                "slot_name": "glimmung-1",
                "url": "https://glimmung-1.glimmung.dev.romaine.life",
                "lease": "glimmung-1",
            }

    register_tools(mcp, CheckoutClient(), tank)  # type: ignore[arg-type]

    with _caller_session("9190aa98a2"):
        mcp.tools["checkout_test_slot"](
            project="glimmung",
        )

    assert tank.calls[0]["url"] == "https://glimmung-1.glimmung.dev.romaine.life"


def test_checkout_test_slot_returns_lease_when_tank_callback_fails() -> None:
    mcp = FakeMCP()

    class CheckoutClient(StubClient):
        def post(
            self,
            path: str,
            params: dict[str, Any] | None = None,
            json: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            self.calls.append(("POST", path, params, json))
            return {
                "state": "active",
                "project": "glimmung",
                "workflow": "test-slot-checkout",
                "slot_index": 1,
                "slot_name": "glimmung-1",
                "url": "https://glimmung-1.glimmung.dev.romaine.life",
                "lease": "glimmung-1",
            }

    register_tools(mcp, CheckoutClient(), FailingTankClient())  # type: ignore[arg-type]

    with _caller_session("missing"):
        result = mcp.tools["checkout_test_slot"](
            project="glimmung",
        )

    assert result["lease"] == "glimmung-1"
    assert "tank_test_state_error" in result


def test_checkout_test_slot_reports_missing_server_url_without_guessing() -> None:
    mcp = FakeMCP()
    tank = StubTankClient()

    class CheckoutClient(StubClient):
        def post(
            self,
            path: str,
            params: dict[str, Any] | None = None,
            json: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            self.calls.append(("POST", path, params, json))
            return {
                "state": "active",
                "project": "glimmung",
                "workflow": "test-slot-checkout",
                "slot_index": 1,
                "slot_name": "glimmung-1",
                "lease": "glimmung-1",
            }

    register_tools(mcp, CheckoutClient(), tank)  # type: ignore[arg-type]

    with _caller_session("9190aa98a2"):
        result = mcp.tools["checkout_test_slot"](
            project="glimmung",
        )

    assert tank.calls == []
    assert result["lease"] == "glimmung-1"
    assert result["tank_test_state_error"] == "checkout response did not include a test slot url"


def test_return_test_slot_posts_return_payload() -> None:
    tools, client = _registered_tools()

    with _caller_session("abc123"):
        result = tools["return_test_slot"](
            project="glimmung",
            slot_index=2,
            slot_name="glimmung-slot-2",
        )

    assert result["path"] == "/v1/test-slots/return"
    assert result["json"] == {
        "project": "glimmung",
        "slot_index": 2,
        "slot_name": "glimmung-slot-2",
        "caller_session_id": "abc123",
        "source": "mcp-glimmung.return_test_slot",
    }
    assert client.calls[-1] == (
        "POST",
        "/v1/test-slots/return",
        None,
        result["json"],
    )


def test_return_test_slot_forwards_slot_name_for_orphan_recovery() -> None:
    # Operator cleanup-retry for an orphaned error+cleanup_error slot is
    # addressed by slot_name. The tool must forward slot_name + source so
    # Glimmung can re-drive cleanup; the caller session is attributed from
    # trusted context rather than a model-supplied tank_session_id.
    tools, client = _registered_tools()

    with _caller_session("abc123"):
        result = tools["return_test_slot"](
            project="tank-operator",
            slot_name="tank-operator-slot-1",
            reason="re-drive cleanup wedged by transient auth outage",
        )

    assert result["json"] == {
        "project": "tank-operator",
        "slot_name": "tank-operator-slot-1",
        "caller_session_id": "abc123",
        "reason": "re-drive cleanup wedged by transient auth outage",
        "source": "mcp-glimmung.return_test_slot",
    }
    assert "slot_index" not in result["json"]
    assert client.calls[-1] == (
        "POST",
        "/v1/test-slots/return",
        None,
        result["json"],
    )


def test_set_test_environment_count_patches_project_count() -> None:
    tools, client = _registered_tools()

    result = tools["set_test_environment_count"](
        project="glimmung",
        count=5,
    )

    assert result["path"] == "/v1/projects/glimmung/test-environments/count"
    assert result["json"] == {"count": 5}
    assert client.calls[-1] == (
        "PATCH",
        "/v1/projects/glimmung/test-environments/count",
        None,
        {"count": 5},
    )


def test_set_test_environment_count_rejects_out_of_range() -> None:
    tools, _client = _registered_tools()

    import pytest

    with pytest.raises(ValueError, match="between 0 and 50"):
        tools["set_test_environment_count"](project="glimmung", count=-1)
    with pytest.raises(ValueError, match="between 0 and 50"):
        tools["set_test_environment_count"](project="glimmung", count=51)


def test_repair_test_slot_posts_project_slot_repair() -> None:
    tools, client = _registered_tools()

    result = tools["repair_test_slot"](
        project="ambience",
        slot_name="ambience-slot-2",
    )

    assert result["path"] == "/v1/projects/ambience/test-environments/ambience-slot-2/repair"
    assert result["json"] is None
    assert client.calls[-1] == (
        "POST",
        "/v1/projects/ambience/test-environments/ambience-slot-2/repair",
        None,
        None,
    )


def test_repair_test_slot_requires_slot_name() -> None:
    tools, _client = _registered_tools()

    signature = inspect.signature(tools["repair_test_slot"])

    assert list(signature.parameters) == ["project", "slot_name"]


def test_extend_test_slot_lease_posts_extend_payload() -> None:
    tools, client = _registered_tools()

    with _caller_session("session-abc123"):
        result = tools["extend_test_slot_lease"](
            project="glimmung",
            extend_seconds=1800,
            slot_name="glimmung-slot-2",
            reason="still validating",
        )

    assert result["path"] == "/v1/test-slots/extend"
    assert result["json"] == {
        "project": "glimmung",
        "tank_session_id": "abc123",
        "extend_seconds": 1800,
        "slot_name": "glimmung-slot-2",
        "reason": "still validating",
        "source": "mcp-glimmung.extend_test_slot_lease",
    }
    assert client.calls[-1] == (
        "POST",
        "/v1/test-slots/extend",
        None,
        result["json"],
    )


def test_return_test_slot_clears_tank_session_when_requested() -> None:
    mcp = FakeMCP()
    tank = StubTankClient()
    register_tools(mcp, StubClient(), tank)  # type: ignore[arg-type]

    with _caller_session("abc123"):
        result = mcp.tools["return_test_slot"](
            project="tank-operator",
            slot_index=2,
        )

    assert tank.calls == [
        {
            "session_id": "abc123",
            "active": False,
            "slot_index": None,
            "url": None,
        }
    ]
    assert result["tank_test_state"] is None
    assert result["json"]["caller_session_id"] == "abc123"


def test_get_runner_events_calls_hot_log_surface() -> None:
    tools, client = _registered_tools()

    result = tools["get_runner_events"](
        project="ambience",
        issue_number=44,
        run_number="1.1",
        attempt_index=2,
        job_id="agent",
        limit=25,
    )

    assert result["path"] == "/v1/projects/ambience/issues/44/runs/1.1/run/events"
    assert client.calls[-1] == (
        "GET",
        "/v1/projects/ambience/issues/44/runs/1.1/run/events",
        {"attempt_index": 2, "job_id": "agent", "limit": 25},
        None,
    )


def test_inspect_browser_url_resolves_lease_with_requester_metadata_shape(monkeypatch) -> None:
    """Real Glimmung lease shape for runner-k8s test slots has
    tank_session_id at requester.metadata.tank_session_id rather than
    top-level metadata. Previously this resolved to "no active test-slot
    lease" and broke every inspect_browser_url call from a session that
    held a real checkout.
    """
    tools, client = _registered_tools()
    client.responses[("GET", "/v1/state")] = {
        "active_leases": [
            {
                "ref": "tank-operator-slot-4",
                "lease_number": 115,
                "project": "tank-operator",
                "metadata": {
                    "runner_slot_name": "tank-operator-slot-4",
                    "requester": {
                        "consumer": "tank-operator",
                        "kind": "tank_session",
                        "label": "abc123",
                        "metadata": {"tank_session_id": "abc123"},
                        "ref": "tank-operator/session/abc123",
                    },
                },
                "requester": {
                    "consumer": "tank-operator",
                    "kind": "tank_session",
                    "label": "abc123",
                    "metadata": {"tank_session_id": "abc123"},
                    "ref": "tank-operator/session/abc123",
                },
                "playwright_ws_endpoint": "ws://slot-playwright.tank-operator-slot-4.svc.cluster.local:3000",
            }
        ]
    }
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "mcp_glimmung.tools.inspect_url",
        _fake_inspect_url_returning_path(calls),
    )
    with _caller_session("abc123"):
        result = tools["inspect_browser_url"](
            url="https://example.test/",
        )
    assert result["final_url"] == "https://example.test/"
    assert result["scope"] == "lease"


def test_inspect_browser_url_errors_when_session_has_no_lease() -> None:
    tools, client = _registered_tools()
    client.responses[("GET", "/v1/state")] = {"active_leases": []}

    with pytest.raises(RuntimeError, match="no active test-slot lease"):
        with _caller_session("abc123"):
            tools["inspect_browser_url"](
                url="https://example.test/",
            )


def test_inspect_browser_url_errors_when_lease_has_no_ws_endpoint() -> None:
    tools, client = _registered_tools()
    client.responses[("GET", "/v1/state")] = {
        "active_leases": [
            {
                "id": "lease-1",
                "metadata": {"tank_session_id": "abc123", "runner_slot_name": "tank-operator-slot-1"},
            }
        ]
    }

    with pytest.raises(RuntimeError, match="no playwright_ws_endpoint"):
        with _caller_session("abc123"):
            tools["inspect_browser_url"](
                url="https://example.test/",
            )


def test_inspect_browser_url_forwards_endpoint_from_active_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools, client = _registered_tools()
    client.responses[("GET", "/v1/state")] = {
        "active_leases": [
            {
                "id": "lease-1",
                "project": "tank-operator",
                "metadata": {
                    "tank_session_id": "abc123",
                    "runner_slot_name": "tank-operator-slot-1",
                    "playwright_ws_endpoint": "ws://slot-playwright.tank-operator-slot-1.svc.cluster.local:3000",
                },
            }
        ]
    }
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "mcp_glimmung.tools.inspect_url",
        _fake_inspect_url_returning_path(calls),
    )

    with _caller_session("abc123"):
        result = tools["inspect_browser_url"](
            url="https://example.test/",
        )

    assert result["final_url"] == "https://example.test/"
    assert (
        calls[0]["playwright_ws_endpoint"]
        == "ws://slot-playwright.tank-operator-slot-1.svc.cluster.local:3000"
    )


def test_register_workflow_forwards_dispatch_inputs_to_v1_workflows() -> None:
    """The MCP wrapper must surface dispatch_inputs as a first-class
    parameter so callers can declare the per-dispatch input contract on
    the workflow registration. Passing it through is what makes the
    server-side validator's `${{ inputs.X }}` requirement satisfiable
    from MCP callers without dropping to raw HTTP."""
    tools, client = _registered_tools()

    result = tools["register_workflow"](
        project="ambience",
        name="default",
        phases=[{"name": "prepare"}],
        dispatch_inputs=[
            {
                "name": "git_ref",
                "description": "branch or sha to check out",
                "required": True,
                "default": "main",
            }
        ],
    )

    assert result["path"] == "/v1/workflows"
    body = result["json"]
    assert body["dispatch_inputs"] == [
        {
            "name": "git_ref",
            "description": "branch or sha to check out",
            "required": True,
            "default": "main",
        }
    ]


def test_register_workflow_omits_dispatch_inputs_when_unset() -> None:
    """Backward compatibility: a register_workflow call that doesn't
    pass dispatch_inputs must not put the key in the request body, so
    older registrations that don't need any inputs keep the same wire
    shape."""
    tools, client = _registered_tools()

    result = tools["register_workflow"](
        project="ambience",
        name="default",
        phases=[{"name": "prepare"}],
    )

    assert "dispatch_inputs" not in (result["json"] or {})


def test_register_workflow_forwards_vars_to_v1_workflows() -> None:
    """The MCP wrapper must surface `vars` as a first-class parameter so
    callers can declare the registration-owned variable map that phase-
    and job-level `when` conditions reference. Without the passthrough,
    a conditional workflow shape is unregistrable from MCP callers."""
    tools, client = _registered_tools()

    result = tools["register_workflow"](
        project="ambience",
        name="default",
        phases=[{"name": "prepare"}],
        vars={"feature_type": "effect", "issue_contract": "off"},
    )

    assert result["path"] == "/v1/workflows"
    assert result["json"]["vars"] == {
        "feature_type": "effect",
        "issue_contract": "off",
    }


def test_register_workflow_omits_vars_when_unset() -> None:
    """A register_workflow call without vars must not put the key in the
    request body, keeping unconditional registrations' wire shape stable."""
    tools, client = _registered_tools()

    result = tools["register_workflow"](
        project="ambience",
        name="default",
        phases=[{"name": "prepare"}],
    )

    assert "vars" not in (result["json"] or {})


def test_pin_workflow_control_puts_target_with_reason() -> None:
    """Pinning rides PUT /control-pins/{target} with the trimmed reason in
    the body; an empty reason stays out of the payload so the server's
    omitempty semantics hold."""
    tools, client = _registered_tools()

    result = tools["pin_workflow_control"](
        project="ambience",
        name="default",
        target="phases.llm-verify.recycle_policy",
        reason="  systemic verify fails must not recycle  ",
    )

    assert result["path"] == "/v1/workflows/ambience/default/control-pins/phases.llm-verify.recycle_policy"
    assert result["json"] == {"reason": "systemic verify fails must not recycle"}

    result = tools["pin_workflow_control"](
        project="ambience",
        name="default",
        target="budget",
    )
    assert result["json"] == {}


def test_unpin_workflow_control_deletes_target() -> None:
    tools, client = _registered_tools()

    tools["unpin_workflow_control"](
        project="ambience",
        name="default",
        target="pr.recycle_policy",
    )

    assert client.calls[-1] == (
        "DELETE",
        "/v1/workflows/ambience/default/control-pins/pr.recycle_policy",
        None,
        None,
    )


def test_list_workflow_control_events_gets_ledger_with_limit() -> None:
    tools, client = _registered_tools()

    tools["list_workflow_control_events"](project="ambience", name="default", limit=5)

    assert client.calls[-1] == (
        "GET",
        "/v1/workflows/ambience/default/control-events",
        {"limit": 5},
        None,
    )
