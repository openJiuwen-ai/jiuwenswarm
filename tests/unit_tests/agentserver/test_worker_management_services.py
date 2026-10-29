"""Domain management shares the Host without loading unrelated execution SDKs."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from jiuwenswarm.server.management.facade import ManagementFacade
from jiuwenswarm.server.management.registry import ManagementServices


async def test_recommendation_host_retains_execution_owner_and_closes_once(monkeypatch):
    from unittest.mock import AsyncMock, Mock
    from jiuwenswarm.server.worker.execution_host import WorkerExecutionHost
    from jiuwenswarm.server.runtime import recommendation_host

    runtime = SimpleNamespace(
        bind_active_requests=Mock(), bind_push=Mock(), start=AsyncMock(), shutdown=AsyncMock(),
    )
    factory = Mock(return_value=runtime)
    monkeypatch.setattr(recommendation_host, "RecommendationHost", factory)
    owner = WorkerExecutionHost.__new__(WorkerExecutionHost)
    owner._recommendation_host = None
    assert owner._recommendation_host_instance() is runtime
    assert owner._recommendation_host_instance() is runtime
    factory.assert_called_once_with()
    runtime.bind_active_requests.assert_called_once_with(owner._recommendation_active_request_ids)
    runtime.bind_push.assert_called_once_with(owner._push_recommendation_event)
    await owner._start_recommendation_host()
    assert recommendation_host.active_recommendation_host() is runtime
    await owner._stop_recommendation_host()
    await owner._stop_recommendation_host()
    assert owner._recommendation_host is None
    assert recommendation_host.active_recommendation_host() is None
    runtime.shutdown.assert_awaited_once()


def test_complete_ui_read_lists_do_not_load_transport_server_or_team_sdk(tmp_path):
    code = r"""
import asyncio
import importlib.abc
import json
import sys

blocked = (
    "jiuwenswarm.server.agent_ws_server",
    "openjiuwen.agent_teams",
    "openjiuwen.rsi",
    "jiuwenswarm.agents.harness.team.team_manager",
    "jiuwenswarm.server.runtime.agent_adapter.team_helpers",
    "jiuwenswarm.server.runtime.agent_adapter.interface_deep",
)
attempts = []
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + ".") for name in blocked):
            attempts.append(fullname)
            raise AssertionError("forbidden UI read dependency: " + fullname)
guard = Guard()
sys.meta_path.insert(0, guard)
from jiuwenswarm.server.worker.execution_host import WorkerExecutionHost
from jiuwenswarm.server.worker.management_adapters import LazyAdapterRegistry
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.common.e2a.wire_codec import parse_agent_server_wire_unary

class Sink:
    def __init__(self): self.rows = []
    async def send(self, payload): self.rows.append(json.loads(payload))

host = WorkerExecutionHost.__new__(WorkerExecutionHost)
host._preparation_registry = None
host._adapter_registry = LazyAdapterRegistry(host)
sink = Sink()
async def run():
    for method in ("a4p.authorization.pending", "command.workflows"):
        request = AgentRequest(request_id=method, channel_id="web",
            req_method=ReqMethod(method), params={})
        await host.dispatch_parsed_request(sink, request, asyncio.Lock())
asyncio.run(run())
assert len(sink.rows) == 2
responses = [parse_agent_server_wire_unary(row) for row in sink.rows]
assert all(response.ok for response in responses), responses
assert responses[1].payload["workflows"] == []
assert not attempts, attempts
assert not any(name == prefix or name.startswith(prefix + ".")
    for prefix in blocked for name in sys.modules)
assert "__getattr__" not in WorkerExecutionHost.__dict__
assert set(host.management._providers) == {"authorization", "workflows"}
print("READONLY_UI_BOUNDARY_OK")
"""
    root = Path(__file__).resolve().parents[3]
    env = os.environ.copy()
    env.update(
        PYTHONPATH=str(root),
        JIUWENSWARM_DATA_DIR=str(tmp_path / "data"),
        JIUWENSWARM_HOME=str(tmp_path / "home"),
        PYTHONUTF8="1",
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "READONLY_UI_BOUNDARY_OK" in result.stdout


def test_provider_failure_retries_without_replacing_resource_owner(monkeypatch):
    from jiuwenswarm.server.management import registry

    calls = []
    host = object()

    class Provider:
        def __init__(self, owner):
            calls.append(owner)
            if len(calls) == 1:
                raise ValueError("temporary catalog failure")
            self.host = owner

    monkeypatch.setattr(
        registry,
        "import_module",
        lambda name: SimpleNamespace(WorkflowsManagement=Provider),
    )
    services = ManagementServices(host)
    with pytest.raises(ValueError, match="temporary"):
        services.provider("workflows")
    assert services._providers == {}
    selected = services.provider("workflows")
    assert selected.host is host
    assert services.provider("workflows") is selected
    assert calls == [host, host]


@pytest.mark.asyncio
async def test_stream_delegate_closes_the_same_provider_generator():
    closed = []

    async def stream(request):
        try:
            yield {"event": "first"}
            yield {"event": "second"}
        finally:
            closed.append(request)

    host = ManagementFacade()
    host._management_services = SimpleNamespace(
        provider=lambda name: SimpleNamespace(_targeted_expert_runtime_stream=stream)
    )
    iterator = host._targeted_expert_runtime_stream("request")
    assert await anext(iterator) == {"event": "first"}
    await iterator.aclose()
    assert closed == ["request"]


def test_recovered_workflow_write_owns_only_workflow_fields(monkeypatch):
    from jiuwenswarm.server.management import workflow_reader
    from jiuwenswarm.server.runtime.session import session_metadata

    metadata = {
        "session_id": "session-workflow",
        "title": "preserved",
        "session_budget": {"remaining": 7},
        "workflow_runs": {"run": {"run_id": "run", "status": "running"}},
    }
    captured = []
    monkeypatch.setattr(
        session_metadata, "_read_metadata", lambda *args, **kwargs: metadata
    )
    monkeypatch.setattr(
        session_metadata,
        "_enqueue_write",
        lambda *args, **kwargs: captured.append((args, kwargs)),
    )
    rows = workflow_reader.restored_workflows("session-workflow")
    assert rows[0]["status"] == "paused"
    assert rows[0]["recovered"] is True
    assert captured[0][1]["merge_fields"] == frozenset({"workflow_runs"})
    assert captured[0][0][1]["title"] == "preserved"
    assert captured[0][0][1]["session_budget"] == {"remaining": 7}


def test_legacy_workflow_models_export_the_shared_schema():
    from jiuwenswarm.agents.harness.team.handlers.workflow_state import WorkflowRunState
    from jiuwenswarm.server.management.workflow_state import (
        WorkflowRunState as SharedWorkflowRunState,
    )

    assert WorkflowRunState is SharedWorkflowRunState


@pytest.fixture
def persisted_workflows(tmp_path, monkeypatch):
    from jiuwenswarm.common import utils
    from jiuwenswarm.server.management.workflow_state import WorkflowRunState
    from jiuwenswarm.server.runtime.session import lifecycle, session_metadata

    sessions = tmp_path / "sessions"
    session_id = "workflow-read"
    folder = sessions / session_id
    folder.mkdir(parents=True)
    monkeypatch.setattr(utils, "get_agent_sessions_dir", lambda: sessions)
    monkeypatch.setattr(lifecycle, "get_agent_sessions_dir", lambda: sessions)
    monkeypatch.setattr(session_metadata, "get_agent_sessions_dir", lambda: sessions)
    runs = {
        "done": WorkflowRunState(id="done", name="finished", status="completed"),
        "active": WorkflowRunState.model_validate(
            {
                "id": "active",
                "name": "recoverable",
                "status": "running",
                "phases": [{"id": "p1", "name": "one"}, {"id": "p2", "name": "two"}],
            }
        ),
    }
    initial = {
        "session_id": session_id,
        "channel_id": "web",
        "mode": "team.work.normal",
        "title": "initial",
        "session_budget": {"remaining": 100},
        "model_selection": {"type": "model", "id": "model-initial"},
        "workflow_runs": {key: value.model_dump() for key, value in runs.items()},
    }
    path = folder / "metadata.json"
    path.write_text(json.dumps(initial), encoding="utf-8")
    yield session_id, path, runs
    assert session_metadata.flush_pending_writes(5.0)


@pytest.mark.asyncio
async def test_persisted_workflow_list_and_detail_keep_pagination_and_recovery(
    persisted_workflows, monkeypatch
):
    from jiuwenswarm.common.e2a.wire_codec import parse_agent_server_wire_unary
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.server.management import workflow_reader
    from jiuwenswarm.server.runtime.session import session_metadata
    from jiuwenswarm.server.worker.execution_host import WorkerExecutionHost

    session_id, path, _ = persisted_workflows
    monkeypatch.setattr(workflow_reader, "existing_workflow_handler", lambda sid: None)
    host = WorkerExecutionHost.__new__(WorkerExecutionHost)
    delivered = []

    async def send(value):
        delivered.append(json.loads(value))

    for params in (
        {"offset": 1, "limit": 1},
        {
            "action": "get_workflow",
            "workflow_id": "active",
            "phase_offset": 1,
            "phase_limit": 1,
        },
    ):
        request = AgentRequest(
            request_id=str(len(delivered)),
            channel_id="web",
            session_id=session_id,
            req_method=ReqMethod.COMMAND_WORKFLOWS,
            params=params,
        )
        await host._handle_command_workflows(
            SimpleNamespace(send=send), request, asyncio.Lock()
        )
    listing, detail = (parse_agent_server_wire_unary(row).payload for row in delivered)
    assert listing["total"] == 2
    assert listing["has_more"] is False
    assert len(listing["workflows"]) == 1
    assert listing["workflows"][0]["id"] == "active"
    assert listing["workflows"][0]["status"] == "paused"
    assert listing["workflows"][0]["recovered"] is True
    assert detail["phase_total"] == 2
    assert detail["has_more"] is False
    assert detail["workflow"]["phases"][0]["id"] == "p2"
    assert session_metadata.flush_pending_writes(5.0)
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["workflow_runs"]["done"]["status"] == "completed"
    assert persisted["workflow_runs"]["done"]["recovered"] is False
    assert persisted["workflow_runs"]["active"]["status"] == "paused"


def test_recovery_does_not_overwrite_concurrent_metadata_fields(
    persisted_workflows, monkeypatch
):
    from jiuwenswarm.server.management.workflow_reader import restored_workflows
    from jiuwenswarm.server.runtime.session import session_metadata

    session_id, path, _ = persisted_workflows
    original_enqueue = session_metadata._enqueue_write
    changed = {
        "title": "updated concurrently",
        "session_budget": {"remaining": 41},
        "model_selection": {"type": "model", "id": "model-new"},
    }

    def concurrent_update_then_enqueue(sid, metadata, **kwargs):
        session_metadata.patch_session_metadata(sid, changed)
        original_enqueue(sid, metadata, **kwargs)

    monkeypatch.setattr(
        session_metadata, "_enqueue_write", concurrent_update_then_enqueue
    )
    assert restored_workflows(session_id)[1]["status"] == "paused"
    assert session_metadata.flush_pending_writes(5.0)
    after = json.loads(path.read_text(encoding="utf-8"))
    assert {name: after[name] for name in changed} == changed
    assert after["workflow_runs"]["active"]["recovered"] is True


@pytest.mark.asyncio
async def test_existing_live_handler_keeps_running_state_without_checkpoint_write(
    persisted_workflows, monkeypatch
):
    from jiuwenswarm.common.e2a.wire_codec import parse_agent_server_wire_unary
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.server.management import workflow_reader
    from jiuwenswarm.server.worker.execution_host import WorkerExecutionHost

    session_id, path, runs = persisted_workflows
    before = path.read_bytes()
    active = runs["active"]
    handler = SimpleNamespace(
        get_workflow_snapshot=lambda: [active.to_workflow_run_dict()]
    )
    monkeypatch.setitem(
        sys.modules,
        "jiuwenswarm.agents.harness.team.team_manager",
        SimpleNamespace(
            _team_manager=SimpleNamespace(get_workflow_handler=lambda sid: handler)
        ),
    )
    monkeypatch.setattr(
        workflow_reader,
        "restored_workflows",
        lambda sid: pytest.fail("live state cannot be restored"),
    )
    host = WorkerExecutionHost.__new__(WorkerExecutionHost)
    delivered = []

    async def send(value):
        delivered.append(json.loads(value))

    request = AgentRequest(
        request_id="live",
        channel_id="web",
        session_id=session_id,
        req_method=ReqMethod.COMMAND_WORKFLOWS,
        params={},
    )
    await host._handle_command_workflows(
        SimpleNamespace(send=send), request, asyncio.Lock()
    )
    payload = parse_agent_server_wire_unary(delivered[0]).payload
    assert payload["workflows"][0]["status"] == "running"
    assert payload["workflows"][0].get("recovered", False) is False
    assert active.status == "running"
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_compatibility_stop_preserves_subclass_constructor_and_cancellation(
    monkeypatch,
):
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
    from jiuwenswarm.server.worker.execution_host import WorkerExecutionHost

    class SpecializedServer(AgentWebSocketServer):
        def __init__(self):
            raise AssertionError("stopping cannot invoke a subclass constructor")

    server = SpecializedServer.__new__(SpecializedServer)
    server._runtime = SimpleNamespace(closed=False)
    server._host = "127.0.0.1"
    server._port = 0
    server._ping_interval = 30.0
    server._ping_timeout = 300.0
    old_runtime = server._runtime
    initialized = []

    async def close_then_cancel(owner):
        old_runtime.closed = True
        raise asyncio.CancelledError

    def initialize_compatibility_owner(owner, *args, **kwargs):
        initialized.append((owner, args, kwargs))
        owner._runtime = SimpleNamespace(closed=False)

    monkeypatch.setattr(WorkerExecutionHost, "stop", close_then_cancel)
    monkeypatch.setattr(
        AgentWebSocketServer, "__init__", initialize_compatibility_owner
    )
    with pytest.raises(asyncio.CancelledError):
        await server.stop()
    assert len(initialized) == 1
    assert initialized[0][0] is server
    assert server._runtime is not old_runtime


@pytest.mark.asyncio
async def test_generated_team_binding_keeps_calling_class_factory_override(monkeypatch):
    from jiuwenswarm.agents.harness import team

    created = []

    async def generate(description, **kwargs):
        return "generated-team"

    monkeypatch.setitem(team.__dict__, "generate_team_name", generate)
    monkeypatch.setitem(
        team.__dict__,
        "list_team_template_summaries",
        lambda config: [{"template_id": "template"}],
    )

    class CustomizedManagement(ManagementFacade):
        @staticmethod
        async def _create_team_binding_from_template(**kwargs):
            created.append(kwargs)
            return "created"

    result = await CustomizedManagement._create_generated_team_binding(
        description="description", config_base={}
    )
    assert result == ("created", {"template_id": "template"})
    assert created == [
        {"team_name": "generated-team", "template_id": "template", "config_base": {}}
    ]
