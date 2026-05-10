import sys
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp_glimmung.tools import register_tools


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

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append(("GET", path, params, None))
        return {"path": path}

    def patch(self, path: str, json: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("PATCH", path, None, json))
        return {"path": path, "json": json}

    def post(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append(("POST", path, params, json))
        return {"path": path, "params": params, "json": json}


class StubTankClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def set_test_environment(
        self,
        caller_pod_ip: str,
        session_id: str,
        *,
        active: bool = True,
        slot_index: int | None = None,
        url: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "caller_pod_ip": caller_pod_ip,
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


def test_list_issues_plain_call_caps_results() -> None:
    tools, client = _registered_tools()

    tools["list_issues"]()

    assert client.calls[-1] == ("GET", "/v1/issues", {"state": "open", "limit": 50}, None)


def test_project_scoped_issue_and_run_tools_call_human_id_surface() -> None:
    tools, client = _registered_tools()

    issue = tools["get_issue_by_number"](project="glimmung", issue_number=141)
    graph = tools["get_issue_graph_by_number"](project="glimmung", issue_number=141)
    report = tools["get_run_report"](project="glimmung", issue_number=141, run_number=1)
    abort = tools["abort_run"](
        project="glimmung", issue_number=141, run_number=1, reason="stuck",
    )

    assert issue["path"] == "/v1/issues/by-number/glimmung/141"
    assert graph["path"] == "/v1/issues/by-number/glimmung/141/graph"
    assert report["path"] == "/v1/projects/glimmung/issues/141/runs/1/report"
    assert abort["path"] == "/v1/projects/glimmung/issues/141/runs/1/abort"
    assert abort["params"] == {"reason": "stuck"}
    assert "abort_run_by_id" not in tools


def test_list_reports_passes_filters_and_defaults_limit() -> None:
    tools, client = _registered_tools()

    tools["list_reports"](
        project="glimmung",
        repo="nelsong6/glimmung",
        state="needs_review",
        limit=10,
    )

    assert client.calls[-1] == (
        "GET",
        "/v1/reports",
        {
            "project": "glimmung",
            "repo": "nelsong6/glimmung",
            "state": "needs_review",
            "limit": 10,
        },
        None,
    )


def test_list_reports_plain_call_caps_results() -> None:
    tools, client = _registered_tools()

    tools["list_reports"]()

    assert client.calls[-1] == ("GET", "/v1/reports", {"limit": 50}, None)


def test_create_pr_posts_registration_payload() -> None:
    tools, client = _registered_tools()

    result = tools["create_report"](
        project="glimmung",
        repo="nelsong6/glimmung",
        number=123,
        title="MCP parity",
        branch="codex/mcp-parity",
        linked_issue_ref="glimmung#123",
        linked_run_ref="glimmung#123/runs/1",
    )

    assert result["path"] == "/v1/reports"
    assert result["json"] == {
        "project": "glimmung",
        "repo": "nelsong6/glimmung",
        "number": 123,
        "title": "MCP parity",
        "branch": "codex/mcp-parity",
        "body": "",
        "base_ref": "main",
        "head_sha": "",
        "html_url": "",
        "linked_issue_ref": "glimmung#123",
        "linked_run_ref": "glimmung#123/runs/1",
    }
    assert client.calls[-1] == ("POST", "/v1/reports", None, result["json"])


def test_raw_id_report_tools_are_not_registered() -> None:
    tools, client = _registered_tools()

    assert "get_report_by_id" not in tools
    assert "list_report_versions" not in tools
    assert "get_report_version" not in tools
    assert "create_report_version" not in tools
    assert "patch_report" not in tools
    assert client.calls == []


def test_project_and_workflow_list_tools_pass_filters_and_default_limits() -> None:
    tools, client = _registered_tools()

    tools["list_projects"](name="glim", github_repo="nelsong6/glimmung")
    tools["list_workflows"](project="glimmung", name="agent", trigger_label="issue-agent")

    assert client.calls[-2:] == [
        ("GET", "/v1/projects", {"name": "glim", "github_repo": "nelsong6/glimmung", "limit": 50}, None),
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
            "host": "native-k8s",
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


def test_check_workflow_updates_calls_upstream_endpoint() -> None:
    tools, client = _registered_tools()

    tools["check_workflow_updates"](project="ambience", workflow="agent-run")

    assert client.calls[-1] == (
        "GET",
        "/v1/projects/ambience/workflows/agent-run/upstream",
        {"ref": "main"},
        None,
    )


def test_check_workflow_updates_passes_ref_for_branch_preview() -> None:
    tools, client = _registered_tools()

    tools["check_workflow_updates"](
        project="ambience", workflow="agent-run", ref="feature/x",
    )

    assert client.calls[-1] == (
        "GET",
        "/v1/projects/ambience/workflows/agent-run/upstream",
        {"ref": "feature/x"},
        None,
    )


def test_sync_workflow_posts_to_sync_endpoint() -> None:
    tools, client = _registered_tools()

    tools["sync_workflow"](project="ambience", workflow="agent-run")

    assert client.calls[-1] == (
        "POST",
        "/v1/projects/ambience/workflows/agent-run/sync",
        {"ref": "main"},
        None,
    )


def test_enqueue_signal_posts_drain_loop_payload() -> None:
    tools, client = _registered_tools()

    result = tools["enqueue_signal"](
        target_type="pr",
        target_repo="nelsong6/glimmung",
        target_ref="nelsong6/glimmung#123",
        payload={"kind": "reject", "feedback": "tighten tests"},
    )

    assert result["path"] == "/v1/signals"
    assert result["json"] == {
        "target_type": "pr",
        "target_repo": "nelsong6/glimmung",
        "target_ref": "nelsong6/glimmung#123",
        "source": "glimmung_ui",
        "payload": {"kind": "reject", "feedback": "tighten tests"},
    }
    assert client.calls[-1] == ("POST", "/v1/signals", None, result["json"])


def test_register_project_and_host_post_admin_payloads() -> None:
    tools, client = _registered_tools()

    project = tools["register_project"](
        "glimmung",
        "nelsong6/glimmung",
        metadata={"tier": "control-plane"},
    )
    host = tools["register_host"](
        "runner-1",
        capabilities={"gpu": False},
        drained=True,
    )

    assert project["json"] == {
        "name": "glimmung",
        "github_repo": "nelsong6/glimmung",
        "metadata": {"tier": "control-plane"},
    }
    assert host["json"] == {
        "name": "runner-1",
        "capabilities": {"gpu": False},
        "drained": True,
    }
    assert client.calls[-2:] == [
        ("POST", "/v1/projects", None, project["json"]),
        ("POST", "/v1/hosts", None, host["json"]),
    ]


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


def test_browser_inspector_tool_uses_shared_inspector(monkeypatch) -> None:
    tools, _client = _registered_tools()
    calls: list[dict[str, Any]] = []

    def fake_inspect_url(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"final_url": kwargs["url"], "elements": []}

    monkeypatch.setattr("mcp_glimmung.tools.inspect_url", fake_inspect_url)

    result = tools["inspect_browser_url"](
        "https://example.test/app",
        viewport={"width": 390, "height": 844},
        wait_ms=100,
        screenshot=False,
    )

    assert result == {"final_url": "https://example.test/app", "elements": []}
    assert calls == [{
        "url": "https://example.test/app",
        "viewport": {"width": 390, "height": 844},
        "wait_ms": 100,
        "timeout_ms": 30000,
        "screenshot": False,
        "full_page": True,
        "capture_accessibility": False,
        "capture_console": True,
        "capture_network": True,
        "max_elements": 80,
        "body_text_limit": 4000,
    }]


def test_resume_run_posts_native_step_boundary_payload() -> None:
    tools, client = _registered_tools()

    result = tools["resume_run"](
        project="glimmung",
        issue_number=141,
        run_number=1,
        entrypoint_phase="agent-execute",
        entrypoint_job_id="agent",
        entrypoint_step_slug="run-agent",
        input_overrides={"namespace": "preview-override"},
        artifact_refs={"repo": "blob://artifacts/source.tgz"},
        context={"operator_note": "resume at agent step"},
        trigger_source={"actor": "codex"},
    )

    assert result["path"] == "/v1/projects/glimmung/issues/141/runs/1/resume"
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
            "resumed_from_run_number": 1,
            "actor": "codex",
        },
    }


def test_checkout_test_slot_posts_checkout_payload() -> None:
    tools, client = _registered_tools()

    result = tools["checkout_test_slot"](
        project="glimmung",
        tank_session_id="abc123",
        workflow="native-agent",
        slot_index=2,
        mode="clean_slate",
        phase_inputs={"image_tag": "sha-123"},
        ttl_seconds=3600,
    )

    assert result["path"] == "/v1/test-slots/checkout"
    assert result["json"] == {
        "project": "glimmung",
        "mode": "clean_slate",
        "requester": {
            "consumer": "tank-operator",
            "kind": "tank_session",
            "ref": "tank-operator/session/abc123",
            "label": "abc123",
            "metadata": {"tank_session_id": "abc123"},
        },
        "tank_session_id": "abc123",
        "workflow": "native-agent",
        "slot_index": 2,
        "phase_inputs": {"image_tag": "sha-123"},
        "ttl_seconds": 3600,
    }
    assert client.calls[-1] == (
        "POST",
        "/v1/test-slots/checkout",
        None,
        result["json"],
    )


def test_checkout_test_slot_updates_tank_session_on_active_slot() -> None:
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
                "project": "tank-operator",
                "workflow": "test-slot-checkout",
                "slot_index": 2,
                "slot_name": "tank-slot-2",
                "lease": "tank-slot-2",
            }

    client = CheckoutClient()
    register_tools(mcp, client, tank)  # type: ignore[arg-type]
    from mcp_glimmung.caller import CALLER_POD_IP

    token = CALLER_POD_IP.set("10.0.0.42")
    try:
        result = mcp.tools["checkout_test_slot"](
            project="tank-operator",
            tank_session_id="abc123",
            slot_index=2,
        )
    finally:
        CALLER_POD_IP.reset(token)

    assert tank.calls == [
        {
            "caller_pod_ip": "10.0.0.42",
            "session_id": "abc123",
            "active": True,
            "slot_index": 2,
            "url": "https://tank-slot-2.tank.dev.romaine.life",
        }
    ]
    assert result["lease"] == "tank-slot-2"
    assert result["tank_test_state"]["slot_index"] == 2
    assert result["tank_session_url"] == "https://tank.example.test/?session=abc123"


def test_checkout_test_slot_accepts_session_pod_name_for_tank_callback() -> None:
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

    client = CheckoutClient()
    register_tools(mcp, client, tank)  # type: ignore[arg-type]
    from mcp_glimmung.caller import CALLER_POD_IP

    token = CALLER_POD_IP.set("10.0.0.42")
    try:
        result = mcp.tools["checkout_test_slot"](
            project="glimmung",
            tank_session_id="session-9190aa98a2",
        )
    finally:
        CALLER_POD_IP.reset(token)

    payload = client.calls[-1][3]
    assert payload is not None
    assert payload["tank_session_id"] == "9190aa98a2"
    assert payload["requester"]["ref"] == "tank-operator/session/9190aa98a2"
    assert tank.calls[0]["session_id"] == "9190aa98a2"
    assert result["tank_session_url"] == "https://tank.example.test/?session=9190aa98a2"


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
                "lease": "glimmung-1",
            }

    register_tools(mcp, CheckoutClient(), FailingTankClient())  # type: ignore[arg-type]
    from mcp_glimmung.caller import CALLER_POD_IP

    token = CALLER_POD_IP.set("10.0.0.42")
    try:
        result = mcp.tools["checkout_test_slot"](
            project="glimmung",
            tank_session_id="missing",
        )
    finally:
        CALLER_POD_IP.reset(token)

    assert result["lease"] == "glimmung-1"
    assert "tank_test_state_error" in result


def test_return_test_slot_posts_return_payload() -> None:
    tools, client = _registered_tools()

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
    }
    assert client.calls[-1] == (
        "POST",
        "/v1/test-slots/return",
        None,
        result["json"],
    )


def test_return_test_slot_clears_tank_session_when_requested() -> None:
    mcp = FakeMCP()
    tank = StubTankClient()
    register_tools(mcp, StubClient(), tank)  # type: ignore[arg-type]
    from mcp_glimmung.caller import CALLER_POD_IP

    token = CALLER_POD_IP.set("10.0.0.42")
    try:
        result = mcp.tools["return_test_slot"](
            project="tank-operator",
            slot_index=2,
            tank_session_id="abc123",
        )
    finally:
        CALLER_POD_IP.reset(token)

    assert tank.calls == [
        {
            "caller_pod_ip": "10.0.0.42",
            "session_id": "abc123",
            "active": False,
            "slot_index": None,
            "url": None,
        }
    ]
    assert result["tank_test_state"] is None


def test_get_native_run_events_calls_hot_log_surface() -> None:
    tools, client = _registered_tools()

    result = tools["get_native_run_events"](
        project="ambience",
        issue_number=44,
        run_number=1,
        attempt_index=2,
        job_id="agent",
        limit=25,
    )

    assert result["path"] == "/v1/projects/ambience/issues/44/runs/1/native/events"
    assert client.calls[-1] == (
        "GET",
        "/v1/projects/ambience/issues/44/runs/1/native/events",
        {"attempt_index": 2, "job_id": "agent", "limit": 25},
        None,
    )
