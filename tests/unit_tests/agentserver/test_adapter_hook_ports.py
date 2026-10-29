# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Host task ports retain permission, output-lease and cancellation ownership."""

from __future__ import annotations

import ast
import asyncio
import builtins
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.runtime.agent_adapter.adapter_hooks import ExtensionAdapterHookServices
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
from jiuwenswarm.server.runtime.agent_adapter.sdk_execution import SDKExecutionDriver


@pytest.mark.parametrize("streaming,ending", [
    (False, "normal"), (False, "error"), (False, "cancel"),
    (True, "normal"), (True, "error"), (True, "cancel"), (True, "close"),
])
async def test_task_binding_stays_inside_permission_context_until_producer_closes(streaming, ending):
    calls = []
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    request = AgentRequest(request_id="request", session_id="session", channel_id="video_tool")
    inputs = {}

    @asynccontextmanager
    async def admission(_request, _inputs):
        calls.append("admission-enter")
        try:
            yield
        finally:
            calls.append("admission-exit")

    @contextmanager
    def permissions(_request):
        calls.append("permission-enter")
        try:
            yield
        finally:
            calls.append("permission-exit")

    class Hooks:
        @asynccontextmanager
        async def bind_task_execution(self, passed_request, passed_adapter, passed_inputs):
            assert (passed_request, passed_adapter, passed_inputs) == (request, adapter, inputs)
            assert calls[-1] == "permission-enter"
            calls.append("binding-enter")
            try:
                yield
            finally:
                calls.append("binding-exit")

    def finish():
        if ending == "error":
            raise ValueError("execution failed")
        if ending == "cancel":
            raise asyncio.CancelledError("execution cancelled")

    async def invoke(_request, _inputs):
        calls.append("execute")
        try:
            finish()
            return "result"
        finally:
            calls.append("producer-exit")

    async def produce(_request, _inputs):
        calls.append("execute")
        try:
            yield "first"
            finish()
        finally:
            calls.append("producer-exit")

    adapter._permission_request_admission = admission
    adapter._bind_permission_request_context = permissions
    adapter.validate_auto_permission_workspace_request = lambda _request: calls.append("validate")
    adapter._process_message_impl = invoke
    adapter._process_message_stream_impl = produce
    adapter.set_hook_services(Hooks())

    async def execute():
        if not streaming:
            assert await adapter.process_message_impl(request, inputs) == "result"
        elif ending == "close":
            iterator = adapter.process_message_stream_impl(request, inputs)
            assert await anext(iterator) == "first"
            await iterator.aclose()
        else:
            assert [item async for item in adapter.process_message_stream_impl(request, inputs)] == ["first"]

    expected = ValueError if ending == "error" else asyncio.CancelledError if ending == "cancel" else None
    if expected:
        with pytest.raises(expected):
            await execute()
    else:
        await execute()
    assert calls == ["admission-enter", "validate", "permission-enter", "binding-enter", "execute",
                     "producer-exit", "binding-exit", "permission-exit", "admission-exit"]


async def test_task_binding_setup_failure_releases_permissions_without_execution():
    calls = []
    adapter = object.__new__(JiuWenSwarmDeepAdapter)

    @asynccontextmanager
    async def admission(*_args):
        try:
            yield
        finally:
            calls.append("admission-close")

    @contextmanager
    def permissions(*_args):
        try:
            yield
        finally:
            calls.append("permission-close")

    class Hooks:
        @asynccontextmanager
        async def bind_task_execution(self, *_args):
            raise PermissionError("task binding rejected")
            yield  # pragma: no cover - defines the async context-manager contract

    adapter.set_hook_services(Hooks())
    adapter._permission_request_admission = admission
    adapter._bind_permission_request_context = permissions
    adapter.validate_auto_permission_workspace_request = Mock()
    adapter._process_message_impl = AsyncMock()
    with pytest.raises(PermissionError, match="task binding rejected"):
        await adapter.process_message_impl(AgentRequest(request_id="rejected"), {})
    adapter._process_message_impl.assert_not_awaited()
    assert calls == ["permission-close", "admission-close"]


async def test_default_task_ports_avoid_imports_for_ordinary_requests(monkeypatch):
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name.startswith("gateway.extensions"):
            pytest.fail("ordinary task ports must not import extension implementations")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    hooks = ExtensionAdapterHookServices()
    request = AgentRequest(request_id="plain", session_id="plain", channel_id="web")
    async with hooks.bind_task_execution(request, object(), {}):
        await hooks.bind_task_output(None, request, object())
        await hooks.close_task_output(None, request)


@pytest.mark.parametrize("ending", ["normal", "error", "cancel"])
async def test_managed_default_binding_preserves_delegate_cleanup(monkeypatch, ending):
    from jiuwenswarm.server.runtime.video_task import execution

    calls = []
    request = AgentRequest(request_id="managed", channel_id="video_tool")
    adapter, inputs = object(), {}

    @asynccontextmanager
    async def binding(passed_request, passed_adapter, passed_inputs):
        assert (passed_request, passed_adapter, passed_inputs) == (request, adapter, inputs)
        calls.append("enter")
        try:
            yield
        finally:
            calls.append("close")

    monkeypatch.setattr(execution, "bind_task_execution", binding)

    async def use():
        async with ExtensionAdapterHookServices().bind_task_execution(request, adapter, inputs):
            calls.append("execute")
            if ending == "error":
                raise ValueError("task failed")
            if ending == "cancel":
                raise asyncio.CancelledError("task cancelled")

    expected = ValueError if ending == "error" else asyncio.CancelledError if ending == "cancel" else None
    if expected:
        with pytest.raises(expected):
            await use()
    else:
        await use()
    assert calls == ["enter", "execute", "close"]


def test_facade_binds_host_services_on_create_and_rebind(monkeypatch):
    from jiuwenswarm.server.runtime.agent_adapter import interface

    hooks, replacement = object(), object()
    backend = SimpleNamespace(set_hook_services=Mock())
    monkeypatch.setattr(interface, "resolve_sdk_choice", lambda: "deep")
    monkeypatch.setattr(interface, "create_adapter", lambda *_args, **_kwargs: backend)
    facade = object.__new__(interface.JiuWenSwarm)
    facade._adapter = None
    facade._skill_manager = SimpleNamespace(set_skillnet_install_complete_hook=Mock())
    facade.set_hook_services(hooks)
    assert facade._ensure_adapter() is backend
    backend.set_hook_services.assert_called_once_with(hooks)
    facade.set_hook_services(replacement)
    assert backend.set_hook_services.call_args.args == (replacement,)


def test_new_and_existing_session_children_share_injected_ports():
    class Adapter(JiuWenSwarmDeepAdapter):
        def __init__(self):
            self._session_assembler = None
            self._channel_id = "web"
            self._is_cron_execution = False
            self._personal_context_runtime_enabled = False
            self._skill_manager = None
            self._heartbeat_service = None
            self._permissions_changed_notifier = None

        def mark_as_session_scoped(self, session_id):
            self.session_id = session_id

        def set_personal_context_runtime_enabled(self, enabled):
            self._personal_context_runtime_enabled = enabled

        def set_heartbeat_service(self, service):
            self._heartbeat_service = service

        def set_permissions_changed_notifier(self, notifier):
            self._permissions_changed_notifier = notifier

    parent = Adapter()
    hooks, replacement = object(), object()
    parent.set_hook_services(hooks)
    assert parent._session_assembler is None  # Binding cannot eagerly assemble Sessions.
    child = parent._new_session_scoped_adapter("session")
    assert child.session_id == "session" and child._get_hook_services() is hooks
    parent._session_assembler = SimpleNamespace(adapters={"session": child})
    parent.set_hook_services(replacement)
    assert child._get_hook_services() is replacement


async def test_failed_rail_install_keeps_the_retained_owner():
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    previous, harness = object(), object()
    adapter._voice_agent_task_rail, adapter._instance = previous, harness
    hook = SimpleNamespace(install_task_rail=AsyncMock(side_effect=ValueError("rail failed")))
    adapter.set_hook_services(hook)
    with pytest.raises(ValueError, match="rail failed"):
        await adapter.install_voice_task_rail(reload=True)
    hook.install_task_rail.assert_awaited_once_with(harness, previous, reload=True)
    assert adapter._voice_agent_task_rail is previous


async def test_default_rail_port_preserves_identity_and_reload_order():
    calls = []

    async def initialized():
        calls.append("initialized")

    async def registered(_rail):
        calls.append("register")

    async def unregistered(_rail):
        calls.append("unregister")

    harness = SimpleNamespace(ensure_initialized=initialized, register_rail=registered, unregister_rail=unregistered)
    hooks = ExtensionAdapterHookServices()
    rail = await hooks.install_task_rail(harness, None)
    assert calls == ["initialized", "register"]
    calls.clear()
    assert await hooks.install_task_rail(harness, rail) is rail
    assert calls == []
    assert await hooks.install_task_rail(harness, rail, reload=True) is rail
    assert calls == ["unregister", "initialized", "register"]


async def test_default_output_port_keeps_exact_lease_and_cancelling_close():
    calls = []
    stream = SimpleNamespace(close=AsyncMock())
    request = AgentRequest(request_id="turn", session_id="managed", channel_id="video_tool",
                           params={"managed_task_request": "turn"})

    async def endpoint_call(action):
        calls.append(action)
        return {"status": "cancelling"}

    binding = SimpleNamespace(request_id="turn", output_stream=None,
        capture_execution=lambda **kwargs: calls.append(("capture", kwargs)),
        endpoint=SimpleNamespace(call=endpoint_call))
    rail = SimpleNamespace(managed_tasks={"managed": binding})
    hooks = ExtensionAdapterHookServices()
    await hooks.bind_task_output(rail, request, stream)
    assert binding.output_stream is stream
    stream.close.assert_awaited_once_with(abort_active_round=False)
    assert calls == [("capture", {"in_callback": False}), "status"]
    await hooks.close_task_output(rail, request)
    assert stream.close.await_count == 2
    assert stream.close.await_args.kwargs == {"abort_active_round": False}


@pytest.mark.parametrize("hook_error", [None, ValueError("host output failed"), asyncio.CancelledError("close cancelled")])
async def test_interrupt_cancels_round_before_port_closes_output_and_keeps_cutover_policy(hook_error):
    calls = []

    async def cancel_round(**_kwargs):
        calls.append("cancel-round")
        return False

    async def close_output(*_args):
        calls.append("close-output")
        if hook_error is not None:
            raise hook_error

    async def complete(_handoff, **kwargs):
        assert kwargs["discard_confirmed"] is True
        calls.append("complete-cutover")
        return True

    owner = SimpleNamespace(
        _resolve_interrupt_session_id=lambda value: value,
        _get_goal_manager=lambda: None,
        _permission_dispatch=SimpleNamespace(start_cutover=AsyncMock(return_value=object()), complete_cutover=complete),
        _cancel_scheduler_running_tasks=lambda: calls.append("cancel-scheduler"),
        _collect_cancelled_tools_for_session=lambda *_args, **_kwargs: [],
        _instance=SimpleNamespace(cancel_round=cancel_round),
        _voice_agent_task_rail=object(),
        _get_hook_services=lambda: SimpleNamespace(close_task_output=close_output),
    )
    request = AgentRequest(request_id="cancel", session_id="session", channel_id="video_tool")
    driver = SDKExecutionDriver(owner, create_session=Mock())
    if isinstance(hook_error, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await driver.interrupt_interaction(request, "cancel", None)
        assert calls == ["cancel-scheduler", "cancel-round", "close-output", "cancel-scheduler"]
    else:
        result = await driver.interrupt_interaction(request, "cancel", None)
        assert result.ok
        assert calls == ["cancel-scheduler", "cancel-round", "close-output", "cancel-scheduler", "complete-cutover"]


def test_execution_core_cannot_import_concrete_swarm_extensions_or_gateway():
    root = Path(__file__).resolve().parents[3]
    package = root / "jiuwenswarm/server/runtime/agent_adapter"
    # Upstream moved these shared value contracts into gateway.common. They
    # carry no Gateway application, transport, configuration or extension owner.
    shared_contracts = {
        "gateway.common.mode_matrix", "gateway.common.model_selection",
        "gateway.common.model_vendor_registry", "gateway.common.session_message",
        "gateway.common.work_mode", "gateway.common.attachment_safety",
        "gateway.common.cron_team_completion",
    }
    failures = []
    for path in package.glob("*.py"):
        if path.name == "adapter_hooks.py":
            continue  # The composition implementation deliberately owns imports.
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = ([node.module] if isinstance(node, ast.ImportFrom) else
                     [alias.name for alias in node.names] if isinstance(node, ast.Import) else [])
            if any(name and name.startswith("gateway") and name not in shared_contracts for name in names):
                failures.append(f"{path.name}:{node.lineno}")
    assert failures == []
