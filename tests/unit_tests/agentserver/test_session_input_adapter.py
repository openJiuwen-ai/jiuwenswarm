# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Receipts must distinguish rejection from a race after SDK submission."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from contextlib import asynccontextmanager, nullcontext

import pytest

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
from jiuwenswarm.server.runtime.agent_adapter.session_input import (
    SessionInputDeliveryUnknown, SessionInputGuard,
)
from jiuwenswarm.runtime.context import reset_runtime_context, set_runtime_context
from jiuwenswarm.runtime.session.model import SessionExecutionState


@pytest.mark.asyncio
@pytest.mark.parametrize("race", ["closed", "changed_round", "rejected", "accepted"])
async def test_receipt_truthfulness_across_sdk_submission(race):
    instance = SimpleNamespace(active_round=object(), has_output_stream=lambda: True)
    guard = SessionInputGuard(instance)
    guard.accepting = race != "rejected"

    async def sdk_send(_request):
        if race == "closed":
            guard.accepting = False
        if race == "changed_round":
            instance.active_round = object()

    instance.send_input = AsyncMock(side_effect=sdk_send)

    async def permission_send(request, *, send):
        await send(request)
        # Existing adapter's bool is permission-transaction ownership, not ACK.
        return False

    adapter = SimpleNamespace(
        _instance=instance, _session_input_guard=guard,
        _stream_completion_state=lambda **kwargs: "completed",
        _prepare_root_input_dispatch=AsyncMock(return_value="prepared"),
        _permission_inputs_for_dispatch=lambda *args: {"query": "extra text"},
        _send_input_with_permission_resume_guard=permission_send,
        _permission_dispatch=SimpleNamespace(finalize=Mock()),
    )
    request = SimpleNamespace(params={"input_mode": "steer"}, request_id="input")
    if race == "accepted":
        assert await JiuWenSwarmDeepAdapter.deliver_active_session_input(
            adapter, request, {"query": "extra text"}
        )
    elif race == "rejected":
        with pytest.raises(RuntimeError, match="not sent"):
            await JiuWenSwarmDeepAdapter.deliver_active_session_input(adapter, request, {})
        instance.send_input.assert_not_awaited()
        adapter._prepare_root_input_dispatch.assert_not_awaited()
        return
    else:
        with pytest.raises(SessionInputDeliveryUnknown, match="do not retry automatically"):
            await JiuWenSwarmDeepAdapter.deliver_active_session_input(
                adapter, request, {"query": "extra text"}
            )
    instance.send_input.assert_awaited_once()
    adapter._permission_dispatch.finalize.assert_called_once_with("prepared")


@pytest.mark.asyncio
async def test_guard_install_failure_is_retryable_and_reload_replaces_registration(monkeypatch):
    instance = SimpleNamespace(
        ensure_initialized=AsyncMock(), register_rail=AsyncMock(side_effect=RuntimeError("rail failed")),
        unregister_rail=AsyncMock(),
        react_agent=SimpleNamespace(register_callback=AsyncMock()),
    )
    adapter = JiuWenSwarmDeepAdapter()
    monkeypatch.setattr(adapter, "_instance", instance)
    with pytest.raises(RuntimeError, match="rail failed"):
        await adapter.install_session_input_guard()
    assert adapter._session_input_guard is None
    instance.register_rail.side_effect = None
    await adapter.install_session_input_guard()
    guard = adapter._session_input_guard
    await adapter.install_session_input_guard()
    assert instance.register_rail.await_count == 2
    await adapter.install_session_input_guard(reload=True)
    instance.unregister_rail.assert_awaited_once_with(guard)
    assert instance.register_rail.await_count == 3
    assert instance.react_agent.register_callback.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("reload", [False, True])
async def test_resume_callback_install_failure_rolls_back_and_can_retry(monkeypatch, reload):
    instance = SimpleNamespace(
        ensure_initialized=AsyncMock(), register_rail=AsyncMock(), unregister_rail=AsyncMock(),
        react_agent=SimpleNamespace(register_callback=AsyncMock()),
    )
    adapter = JiuWenSwarmDeepAdapter()
    monkeypatch.setattr(adapter, "_instance", instance)
    if reload:
        await adapter.install_session_input_guard()
    instance.react_agent.register_callback.side_effect = RuntimeError("callback failed")
    with pytest.raises(RuntimeError, match="callback failed"):
        await adapter.install_session_input_guard(reload=reload)
    assert adapter._session_input_guard is None
    assert instance.unregister_rail.await_count == (2 if reload else 1)
    instance.react_agent.register_callback.side_effect = None
    await adapter.install_session_input_guard()
    assert adapter._session_input_guard is not None
    assert instance.react_agent.register_callback.await_count == (3 if reload else 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["resume", "ordinary", "outer", "already_bound", "no_queue"])
async def test_resume_queue_binding_is_scoped_and_preserves_sdk_queue(case):
    import asyncio
    from openjiuwen.core.session import InteractiveInput
    from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, InvokeInputs
    from openjiuwen.harness.task_loop.loop_queues import LoopQueues

    queues = LoopQueues()
    queues.push_steer("supplement")
    instance = SimpleNamespace(react_agent=object(), event_handler=SimpleNamespace(interaction_queues=queues))
    ctx = AgentCallbackContext(
        agent=instance if case == "outer" else instance.react_agent,
        inputs=InvokeInputs(query="ordinary" if case == "ordinary" else InteractiveInput()),
    )
    existing_queue = asyncio.Queue() if case == "already_bound" else None
    if existing_queue is not None:
        ctx.bind_steering_queue(existing_queue)
    if case == "no_queue":
        instance.event_handler.interaction_queues = None
    await SessionInputGuard(instance).before_invoke(ctx)
    assert ctx.steering_queue is (queues.steering if case == "resume" else existing_queue)
    assert queues.steering.qsize() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("race", ["accepted", "ended", "changed_round", "cancelled", "other_session", "missing_queue"])
async def test_bound_steer_checks_target_at_sdk_enqueue_without_idle_dispatch(race):
    from openjiuwen.harness.task_loop.loop_queues import LoopQueues
    from openjiuwen.harness.task_loop.task_loop_controller import TaskLoopController

    queues = LoopQueues()
    controller = TaskLoopController()
    controller.set_event_handler(SimpleNamespace(interaction_queues=queues))
    instance = SimpleNamespace(
        active_round=object(), has_output_stream=lambda: True,
        loop_controller=controller, send_input=AsyncMock(),
    )
    execution = SimpleNamespace(
        session_id="session", state=SessionExecutionState.RUNNING, cancellation_requested=False,
    )
    guard = SessionInputGuard(instance)
    guard.accepting = True

    async def prepare(*_args):
        # The task can finish/change during awaited permission preparation.
        if race == "ended":
            execution.state = SessionExecutionState.SUCCEEDED
        elif race == "changed_round":
            instance.active_round = object()
        elif race == "cancelled":
            execution.cancellation_requested = True
        elif race == "other_session":
            execution.session_id = "another-session"
        elif race == "missing_queue":
            controller.event_handler.interaction_queues = None
        return {"query": "supplement"}

    async def permission_send(sdk_request, *, send):
        await send(sdk_request)

    adapter = SimpleNamespace(
        _instance=instance, _session_input_guard=guard,
        _stream_completion_state=lambda **kwargs: "completed",
        _prepare_root_input_dispatch=prepare,
        _permission_inputs_for_dispatch=lambda _req, prepared, _mode: prepared,
        _send_input_with_permission_resume_guard=permission_send,
        _permission_dispatch=SimpleNamespace(finalize=Mock()),
    )
    request = SimpleNamespace(
        params={"input_mode": "steer", "expected_execution_id": "execution-A"},
        request_id="supplement-request", session_id="session",
    )
    runtime = SimpleNamespace(get_session_execution=Mock(return_value=execution))
    token = set_runtime_context(runtime, None)
    try:
        if race == "accepted":
            assert await JiuWenSwarmDeepAdapter.deliver_active_session_input(adapter, request, {})
            assert queues.drain_steering() == ["supplement"]
        else:
            with pytest.raises(RuntimeError, match="not sent"):
                await JiuWenSwarmDeepAdapter.deliver_active_session_input(adapter, request, {})
            assert queues.drain_steering() == []
        assert queues.drain_follow_up() == []
        instance.send_input.assert_not_awaited()
        runtime.get_session_execution.assert_called_once_with("execution-A")
        adapter._permission_dispatch.finalize.assert_called_once()
    finally:
        reset_runtime_context(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("is_stream", [False, True])
async def test_bound_input_cannot_fall_back_after_sdk_round_ends(monkeypatch, is_stream):
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    @asynccontextmanager
    async def admission(*_args):
        yield

    monkeypatch.setattr(interface_deep, "setup_permission_context", Mock())
    monkeypatch.setattr(interface_deep, "cleanup_permission_context", Mock())
    adapter = SimpleNamespace(
        _is_session_scoped_adapter=True, _parent_session_id="session", _instance=None,
        _session_adapter_key=lambda value: value,
        _register_session_agent_task=Mock(), _unregister_session_agent_task=Mock(),
        _permission_request_admission=admission,
        validate_auto_permission_workspace_request=Mock(),
        _bind_permission_request_context=lambda _request: nullcontext(),
        deliver_active_session_input=AsyncMock(return_value=False),
        process_message_impl=AsyncMock(), process_message_stream_impl=Mock(),
    )
    request = SimpleNamespace(
        params={"input_mode": "steer", "expected_execution_id": "execution-A"},
        session_id="session", is_stream=is_stream,
    )
    with pytest.raises(RuntimeError, match="targeted execution has ended"):
        async for _chunk in JiuWenSwarmDeepAdapter.deliver_session_input_impl(adapter, request, {}):
            pytest.fail("An ended target must not emit accepted or output")
    adapter.process_message_impl.assert_not_awaited()
    adapter.process_message_stream_impl.assert_not_called()
    adapter._unregister_session_agent_task.assert_called_once_with("session")
