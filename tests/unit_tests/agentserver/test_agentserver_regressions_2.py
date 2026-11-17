# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Consolidated regression tests (merged from 5 small test modules)."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import pytest
from jiuwenswarm.server.runtime import agent_manager as manager_module
import asyncio
from typing import Any
from openjiuwen.core.foundation.llm import ToolCall
from openjiuwen.core.foundation.tool import LocalFunction, ToolCard
from openjiuwen.core.single_agent.ability_manager import (
    AbilityExecutionError,
    AbilityManager,
)
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    ModelCallInputs,
)
from openjiuwen.harness.prompts import SystemPromptBuilder
from openjiuwen.harness.rails.tool_call_resilience_rail import ToolCallResilienceRail
from jiuwenswarm.agents.harness.common.rails.symphony import (
    SymphonyOrchestrationRail,
)
import uuid
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.worker import dispatcher as worker_dispatcher
from jiuwenswarm.server.worker.gateway_bridge import ConnHolder, WorkerGatewayBridge
import json
from pathlib import Path
import psutil
from jiuwenswarm.common import utils
from jiuwenswarm.server.front.event_forwarder import EventForwarder
from jiuwenswarm.server.ipc.client import endpoint_is_listening
from jiuwenswarm.server.lifecycle import Readiness
from jiuwenswarm.server.supervisor.runtime_supervisor import RuntimeSupervisor
from jiuwenswarm.server.ipc.protocol import KIND_REQUEST, dump_agent_request
from jiuwenswarm.server.worker.dispatcher import WorkerDispatcher
from jiuwenswarm.server.worker.preparation import PreparationRegistry
from jiuwenswarm.server.worker.signals import RuntimeSignalSink


# --- from test_agent_manager_project_dir_case.py ---

@pytest.fixture
def facade(monkeypatch):
    agent = Mock()
    agent.create_instance = AsyncMock()
    agent.cleanup = AsyncMock()
    agent.prepare_session = AsyncMock()
    factory = Mock(return_value=agent)
    monkeypatch.setitem(
        sys.modules,
        "jiuwenswarm.server.runtime.agent_adapter.interface",
        SimpleNamespace(JiuWenSwarm=factory),
    )
    monkeypatch.setitem(
        sys.modules,
        "jiuwenswarm.server.runtime.agent_adapter.agent_adapters",
        SimpleNamespace(resolve_sdk_choice=lambda: "test"),
    )
    return agent, factory

@pytest.mark.asyncio
@pytest.mark.parametrize("relative", [False, True])
async def test_runtime_path_preserves_case_across_cache_lookup_and_recreation(
    monkeypatch, tmp_path, facade, relative,
):
    monkeypatch.chdir(tmp_path)
    project = tmp_path / "Straße MiXeD Project"
    project.mkdir()
    declared = project.name if relative else str(project)
    agent, factory = facade
    manager = manager_module.AgentManager()

    assert await manager.get_agent("tui", project_dir=declared) is agent
    config = agent.create_instance.call_args.args[0]
    assert config["project_dir"] == str(project)
    assert await manager.get_agent("tui", project_dir=str(project).swapcase()) is agent
    assert manager.get_agent_nowait("tui", mode="agent", project_dir=str(project).swapcase()) is agent
    factory.assert_called_once()

    await manager.recreate_agent("tui")

    assert factory.call_count == 2
    assert agent.create_instance.call_args.args[0]["project_dir"] == str(project)

@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["project_dir", "workspace_dir", "metadata"])
async def test_auto_request_prepares_session_with_original_path_case(
    monkeypatch, tmp_path, facade, field,
):
    # Exercise Windows-like folding on every platform, including Linux CI.
    monkeypatch.setattr(os.path, "normcase", str.casefold)
    monkeypatch.setattr(
        manager_module, "get_config",
        lambda: {"permissions": {"enabled": True, "mode": "auto"}},
    )
    project = tmp_path / "Straße MiXeD Project"
    project.mkdir()
    params = {"mode": "agent"}
    metadata = {}
    if field == "metadata":
        metadata["project_dir"] = str(project)
    else:
        params[field] = str(project)
    request = SimpleNamespace(channel_id="web", session_id="", params=params, metadata=metadata)
    manager = manager_module.AgentManager()
    agent, _factory = facade
    get_agent = AsyncMock(return_value=agent)
    monkeypatch.setattr(manager, "get_agent", get_agent)

    assert await manager.get_agent_for_request(request) is agent

    assert get_agent.call_args.kwargs["project_dir"] == str(project.resolve())
    assert agent.prepare_session.call_args.kwargs["project_dir"] == str(project.resolve())

# --- from test_symphony_timeout_lifecycle.py ---

class _LifecycleCallbackManager:
    """Run the registered rails through AgentCallbackContext.fire()."""

    def __init__(self, rails: list[Any]) -> None:
        self._rails = sorted(rails, key=lambda rail: rail.priority, reverse=True)
        self.contexts: list[AgentCallbackContext] = []

    async def execute(self, event, ctx: AgentCallbackContext) -> AgentCallbackContext:
        self.contexts.append(ctx)
        for rail in self._rails:
            callback = rail.get_callbacks().get(event)
            if callback is not None:
                await callback(ctx)
        return ctx

class _ResourceManager:
    def __init__(self, tool: LocalFunction) -> None:
        self._tool = tool

    def get_tool(self, *, tool_id: str, **kwargs: Any) -> LocalFunction | None:
        del kwargs
        return self._tool if tool_id == self._tool.card.id else None

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name",
    ["symphony_compose_graph", "symphony_refresh_graph"],
)
async def test_outer_symphony_timeout_terminates_without_retry(
    monkeypatch,
    tool_name,
):
    calls = 0
    force_finish_calls: list[AgentCallbackContext] = []
    original_request_force_finish = AgentCallbackContext.request_force_finish

    def track_force_finish(self, result):
        force_finish_calls.append(self)
        original_request_force_finish(self, result)

    monkeypatch.setattr(
        AgentCallbackContext,
        "request_force_finish",
        track_force_finish,
    )

    async def blocking_tool() -> dict[str, object]:
        nonlocal calls
        calls += 1
        await asyncio.Event().wait()

    card = ToolCard(
        id=tool_name,
        name=tool_name,
        description=tool_name,
        input_params={"type": "object", "properties": {}},
        properties={"resilience": {"timeout_s": None}},
    )
    tool = LocalFunction(card=card, func=blocking_tool)
    resource_manager = _ResourceManager(tool)
    monkeypatch.setattr(
        "openjiuwen.core.runner.Runner.resource_mgr",
        resource_manager,
        raising=False,
    )
    monkeypatch.setattr(
        "openjiuwen.core.single_agent.ability_manager.MAX_TOOL_CALL_TIMEOUT_HARD_LIMIT",
        0.01,
    )

    ability_manager = AbilityManager()
    ability_manager.add(card)
    orchestration_rail = SymphonyOrchestrationRail(
        config_base={"symphony": {"enabled": True}},
    )
    resilience_rail = ToolCallResilienceRail()
    callback_manager = _LifecycleCallbackManager(
        [orchestration_rail, resilience_rail]
    )
    agent = SimpleNamespace(
        ability_manager=ability_manager,
        agent_callback_manager=callback_manager,
        system_prompt_builder=SystemPromptBuilder(language="cn"),
    )
    orchestration_rail.init(agent)
    invoke_ctx = AgentCallbackContext(agent=agent, extra={})
    tool_call = ToolCall(
        id=f"{tool_name}-call",
        type="function",
        name=tool_name,
        arguments="{}",
    )

    results = await ability_manager.execute(
        invoke_ctx,
        tool_call,
        session=None,
        parallel_tool_calls=False,
    )

    assert calls == 1
    payload, _tool_message = results[0]
    assert payload["success"] is False
    assert payload["reason"] == "graph_build_timeout"
    assert "planned_graph" not in payload
    assert payload["direct_display"] is True
    assert payload["continue_after_display"] is False
    assert payload["followup_action"] == "manual_graph_build"
    force_finish = invoke_ctx.consume_force_finish()
    assert force_finish is not None
    assert force_finish.result == {
        "output": payload["content"],
        "result_type": "answer",
    }
    assert force_finish_calls.count(invoke_ctx) == 1
    assert len(force_finish_calls) == 2

    tool_ctx = next(
        ctx
        for ctx in callback_manager.contexts
        if ctx.extra.get("symphony_graph_build_timeout") is True
    )
    assert tool_ctx is not invoke_ctx
    assert "symphony_graph_build_timeout" not in invoke_ctx.extra

    new_invoke_model_ctx = AgentCallbackContext(
        agent=agent,
        inputs=ModelCallInputs(
            tools=[
                SimpleNamespace(name="symphony_compose_graph"),
                SimpleNamespace(name="symphony_refresh_graph"),
            ]
        ),
        extra={},
    )
    await orchestration_rail.before_model_call(new_invoke_model_ctx)
    assert [
        orchestration_rail._model_tool_name(tool)
        for tool in new_invoke_model_ctx.inputs.tools
    ] == ["symphony_compose_graph", "symphony_refresh_graph"]
    assert "## Skill Orchestration Contract" in agent.system_prompt_builder.build()

    exceptions = {
        id(ctx.exception): ctx.exception
        for ctx in callback_manager.contexts
        if ctx.exception is not None
    }
    assert len(exceptions) == 1
    exception = next(iter(exceptions.values()))
    assert isinstance(exception, AbilityExecutionError)
    assert isinstance(exception.__cause__, TimeoutError)

# --- from test_worker_dispatcher_external_cli.py ---

def _request() -> AgentRequest:
    return AgentRequest(
        request_id=uuid.uuid4().hex,
        channel_id="web",
        session_id="sess-1",
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "hello"},
        is_stream=False,
        agent_ref=None,
        user_id="user-1",
    )

def _bare_dispatcher(runtime: object, holder_generation: int = 1) -> worker_dispatcher.WorkerDispatcher:
    """Build a dispatcher with only the attributes ``_run_request`` touches."""
    instance = worker_dispatcher.WorkerDispatcher.__new__(worker_dispatcher.WorkerDispatcher)
    holder = ConnHolder()
    holder.generation = holder_generation
    instance._bridge = WorkerGatewayBridge(holder)
    instance._state_lock = asyncio.Lock()
    instance._lock = asyncio.Lock()
    instance._draining = False
    instance._runtime = runtime
    instance._ready = True
    instance._failure = None
    instance._queued = []
    instance._startup_priorities = {}
    instance._request_tasks = {}
    return instance

@pytest.mark.asyncio
async def test_run_request_refreshes_external_cli_paths_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn must see SDKs installed after the resident worker started."""
    activated: list[bool] = []
    dispatched: list[AgentRequest] = []

    class _Runtime:
        async def dispatch_parsed_request(self, bridge, request, lock):
            dispatched.append(request)

    monkeypatch.setattr(
        worker_dispatcher,
        "activate_external_cli_runtime_paths",
        lambda: activated.append(True),
    )
    instance = _bare_dispatcher(_Runtime())

    await instance._run_request(_request(), 1)

    assert activated == [True]
    assert len(dispatched) == 1

@pytest.mark.asyncio
async def test_run_request_serves_despite_activation_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An optional SDK problem must not fail the request."""
    dispatched: list[AgentRequest] = []

    class _Runtime:
        async def dispatch_parsed_request(self, bridge, request, lock):
            dispatched.append(request)

    def _raise_activation() -> None:
        raise RuntimeError("broken managed runtime layout")

    monkeypatch.setattr(
        worker_dispatcher, "activate_external_cli_runtime_paths", _raise_activation
    )
    instance = _bare_dispatcher(_Runtime())

    await instance._run_request(_request(), 1)

    assert len(dispatched) == 1

# --- from test_worker_session_owner_recovery.py ---

class Gateway:
    def __init__(self):
        self.messages = asyncio.Queue()

    async def send(self, wire):
        await self.messages.put(json.loads(wire))

async def execute(supervisor, content, *, expect_error=False):
    gateway = Gateway()
    lock = asyncio.Lock()
    supervisor.attach_gateway_connection(gateway, lock)
    request = AgentRequest(
        request_id=uuid.uuid4().hex, channel_id="web", session_id="persisted",
        req_method=ReqMethod.CHAT_SEND, params={"content": content},
    )
    await supervisor.dispatch_parsed_request(gateway, request, lock)
    async with asyncio.timeout(15):
        while True:
            message = await gateway.messages.get()
            if message.get("request_id") == request.request_id:
                if expect_error and message.get("response_kind") == "e2a.error":
                    return message
                assert message.get("response_kind") != "e2a.error", message
                if message.get("phase") == "completed":
                    return message

@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", [
    "different-build", "exited-and-reaped", "live-owner",
    pytest.param("unreaped-zombie", marks=pytest.mark.skipif(
        sys.platform != "linux", reason="requires the Linux kernel's unreaped zombie state",
    )),
])
async def test_replacement_worker_reclaims_and_executes_persisted_session(monkeypatch, tmp_path, replacement):
    root = Path(__file__).resolve().parents[3]
    bootstrap = tmp_path / "bootstrap"
    bootstrap.mkdir()
    (bootstrap / "sitecustomize.py").write_text(
        "import sys\n"
        "if 'jiuwenswarm.server.worker.main' in getattr(sys, 'orig_argv', []):\n"
        "    from session_owner_worker import configure\n"
        "    configure()\n", encoding="utf-8",
    )
    paths = [str(bootstrap), str(Path(__file__).parent / "fixtures"), str(root)]
    if os.environ.get("PYTHONPATH"):
        paths.append(os.environ["PYTHONPATH"])
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(paths))
    data = tmp_path / "data"
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(data))
    monkeypatch.setattr(utils, "_workspace_base_dir", data)
    monkeypatch.setenv("JIUWENSWARM_IDENTITY_CACHE_DIR", str(tmp_path / "identity"))
    monkeypatch.setenv("JIUWENSWARM_BUILD_ID", "owner-recovery-old-build")
    session = data / "agent" / "sessions" / "persisted"
    session.mkdir(parents=True)
    metadata = '{"session_id":"persisted","channel_id":"web","title":"Keep me"}'
    (session / "metadata.json").write_text(metadata, encoding="utf-8")
    original = {"content": "original persisted history"}
    (session / "history.jsonl").write_text(json.dumps(original) + "\n", encoding="utf-8")
    owner_path = data / "agent" / "lifecycle" / "resources" / "session_persisted.json"
    endpoint = (str(tmp_path / "worker.pipe") if os.name == "nt"
                else f"/tmp/jw-owner-{uuid.uuid4().hex[:8]}.sock")
    first = RuntimeSupervisor(Readiness(), forwarder=EventForwarder(), socket_path=endpoint, max_restarts=0)
    second_endpoint = endpoint + "-contender" if replacement == "live-owner" else endpoint
    second = RuntimeSupervisor(Readiness(), forwarder=EventForwarder(), socket_path=second_endpoint, max_restarts=0)
    process = None
    try:
        await asyncio.wait_for(first.run_until_ipc_ready(), 20)
        process = first._process
        assert await first._readiness.wait_agent_ready(30)
        before = await execute(first, "before replacement")
        old_pid = first.worker_pid
        old_process = psutil.Process(old_pid)
        old_created = old_process.create_time()
        assert before["pid"] == old_pid
        assert before["history_before"] == [original]
        assert json.loads(owner_path.read_text(encoding="utf-8"))["runtime_owner"]["pid"] == old_pid
        if replacement == "live-owner":
            await asyncio.wait_for(second.run_until_ipc_ready(), 30)
            assert await second._readiness.wait_agent_ready(30)
            assert second.worker_pid != old_pid
            rejected = await execute(second, "must not be written", expect_error=True)
            assert "another live AgentServer owns the session runtime" in json.dumps(rejected)
            assert json.loads(owner_path.read_text(encoding="utf-8"))["runtime_owner"]["pid"] == old_pid
            continued = await execute(first, "original owner still works")
            assert continued["pid"] == old_pid
            assert [item["content"] for item in continued["history_before"]] == [
                original["content"], "before replacement",
            ]
            return
        # Detach the Front so its process watcher cannot reap the old child.
        # Retain the Popen owner until finally; Linux PID 1 is never asked to reap it.
        await first.stop()
        if replacement != "different-build":
            assert old_process.create_time() == old_created
            old_process.kill()
            if replacement == "exited-and-reaped":
                assert await process.wait(timeout=10) is not None
            else:
                async with asyncio.timeout(10):
                    # The leader can become Z before the remaining threads
                    # release their shared listener and lifetime lease.
                    while (old_process.status() != psutil.STATUS_ZOMBIE
                           or old_process.num_threads() != 1
                           or endpoint_is_listening(endpoint)):
                        await asyncio.sleep(.01)
                assert old_process.create_time() == old_created
            assert json.loads(owner_path.read_text(encoding="utf-8"))["runtime_owner"]["pid"] == old_pid
        monkeypatch.setenv("JIUWENSWARM_BUILD_ID", "owner-recovery-new-build")
        await asyncio.wait_for(second.run_until_ipc_ready(), 30)
        assert await second._readiness.wait_agent_ready(30)
        assert second.worker_pid != old_pid
        after = await execute(second, "after replacement")
        assert after["pid"] == second.worker_pid
        assert after["session_id"] == "persisted"
        assert [item["content"] for item in after["history_before"]] == [
            original["content"], "before replacement",
        ]
        assert json.loads(owner_path.read_text(encoding="utf-8"))["runtime_owner"]["pid"] == second.worker_pid
        assert (session / "metadata.json").read_text(encoding="utf-8") == metadata
        history = [json.loads(line)["content"] for line in (session / "history.jsonl").read_text(encoding="utf-8").splitlines()]
        assert history == [original["content"], "before replacement", "after replacement"]
        if replacement == "unreaped-zombie":
            assert old_process.status() == psutil.STATUS_ZOMBIE
            assert old_process.num_threads() == 1
    finally:
        try:
            await second.stop(terminate_worker=True)
        finally:
            try:
                await first.stop(terminate_worker=True)
            finally:
                if process is not None:
                    await process.stop(timeout=5)
                    assert process.returncode is not None

# --- from test_worker_startup_priority.py ---

@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["sdk_entry", "dispatch_failure", "shutdown", "core_failure", "cancel"])
async def test_queued_request_priority_precedes_optional_work_and_is_always_released(outcome):
    holder = ConnHolder()
    dispatcher = WorkerDispatcher(WorkerGatewayBridge(holder))
    preparation = PreparationRegistry()
    mailbox_entered = asyncio.Event()
    mailbox_release = asyncio.Event()
    request_entered = asyncio.Event()
    request_release = asyncio.Event()
    optional_entered = asyncio.Event()
    preparation.register("optional", optional_entered.set)

    class Mailbox:
        async def set_available(self, _available):
            mailbox_entered.set()
            await mailbox_release.wait()

    class Runtime:
        _session_message_service = Mailbox()
        _preparation_registry = preparation
        _startup_preparation_tokens = set()
        _execution_minimum_observed = False

        def attach_gateway_connection(self, _ws, _lock):
            pass

        async def dispatch_parsed_request(self, _ws, _request, _lock):
            request_entered.set()
            if outcome == "dispatch_failure":
                raise RuntimeError("failed before entering SDK")
            await request_release.wait()

    runtime = Runtime()
    await dispatcher.bind_runtime(runtime)
    accepted = AgentRequest(request_id="cold", channel_id="web", session_id="a",
                            req_method=ReqMethod.CHAT_SEND, params={"session_id": "a"})
    await dispatcher.handle({"kind": KIND_REQUEST, "dispatch_id": "cold-dispatch",
                             "payload": dump_agent_request(accepted)}, None)
    await asyncio.sleep(0)
    assert dispatcher._queued
    signals = RuntimeSignalSink(holder, on_ready=dispatcher.mark_ready)
    signals.runtime_base_ready()
    preparation.mark_minimum_ready()
    try:
        await asyncio.wait_for(mailbox_entered.wait(), 1)
        await asyncio.sleep(0)
        assert not optional_entered.is_set()
        assert preparation._foreground_tokens
        if outcome == "shutdown":
            assert await dispatcher.drain(timeout=1)
        elif outcome == "core_failure":
            await dispatcher.mark_failed("core startup failed")
        elif outcome == "cancel":
            cancel = AgentRequest(request_id="cancel", channel_id="web", session_id="a",
                                  req_method=ReqMethod.CHAT_CANCEL, params={"session_id": "a"})
            assert await dispatcher._drop_queued_cancel(cancel)
        mailbox_release.set()
        if outcome in {"sdk_entry", "dispatch_failure"}:
            await asyncio.wait_for(request_entered.wait(), 1)
            if outcome == "sdk_entry":
                assert not optional_entered.is_set()
                # Mirror the actual SDK observer: all initial request tokens
                # release as soon as model, Rails and context are prepared.
                for token in runtime._startup_preparation_tokens:
                    preparation.resume_background(token)
                runtime._startup_preparation_tokens = set()
                runtime._execution_minimum_observed = True
                request_release.set()
        await asyncio.wait_for(optional_entered.wait(), 1)
        await asyncio.sleep(0)
        assert preparation._foreground_tokens == set()
        assert runtime._startup_preparation_tokens == set()
        assert dispatcher._startup_priorities == {}
    finally:
        mailbox_release.set()
        request_release.set()
        await dispatcher.drain(timeout=1)
        await preparation.close()

@pytest.mark.asyncio
async def test_mailbox_readiness_failure_releases_queued_startup_priority():
    preparation = PreparationRegistry()
    dispatcher = WorkerDispatcher(WorkerGatewayBridge(ConnHolder()))

    class Mailbox:
        async def set_available(self, _available):
            await asyncio.sleep(0)
            raise RuntimeError("mailbox startup failed")

    class Runtime:
        _session_message_service = Mailbox()
        _preparation_registry = preparation
        _startup_preparation_tokens = set()

    runtime = Runtime()
    await dispatcher.bind_runtime(runtime)
    accepted = AgentRequest(request_id="cold", channel_id="web", session_id="a",
                            req_method=ReqMethod.CHAT_SEND, params={"session_id": "a"})
    await dispatcher.handle({"kind": KIND_REQUEST, "dispatch_id": "cold-dispatch",
                             "payload": dump_agent_request(accepted)}, None)
    await asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="mailbox startup failed"):
        await dispatcher.mark_ready()
    assert preparation._foreground_tokens == set()
    assert runtime._startup_preparation_tokens == set()
    assert dispatcher._startup_priorities == {}
    assert await dispatcher.drain(timeout=1)
    await preparation.close()

