# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for JiuWenSwarmDeepAdapter interrupt when stream consumer already unwound."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from openjiuwen.core.single_agent.interrupt.state import INTERRUPTION_KEY
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


def _build_cancel_request(session_id: str = "tui_sess_1") -> AgentRequest:
    return AgentRequest(
        request_id="req-cancel",
        channel_id="tui",
        session_id=session_id,
        req_method=ReqMethod.CHAT_CANCEL,
        params={"intent": "cancel", "mode": "agent.plan"},
    )


def _build_supplement_request(session_id: str = "tui_sess_1") -> AgentRequest:
    return AgentRequest(
        request_id="req-supplement",
        channel_id="tui",
        session_id=session_id,
        req_method=ReqMethod.CHAT_CANCEL,
        params={
            "intent": "supplement",
            "new_input": "再执行一次",
            "mode": "agent.plan",
        },
    )


def _build_chat_send_request(
    params: dict,
    session_id: str = "tui_sess_1",
) -> AgentRequest:
    return AgentRequest(
        request_id="req-send",
        channel_id="desktop",
        session_id=session_id,
        req_method=ReqMethod.CHAT_SEND,
        params=params,
    )


def _interruption_state(*tool_names: str) -> SimpleNamespace:
    tool_calls = [
        SimpleNamespace(id=f"call-{index}", name=tool_name)
        for index, tool_name in enumerate(tool_names)
    ]
    return SimpleNamespace(
        ai_message=SimpleNamespace(tool_calls=tool_calls),
        interrupted_tools={
            f"call-{index}": SimpleNamespace(
                tool_call=tool_call,
            )
            for index, tool_call in enumerate(tool_calls)
        },
    )


def _make_adapter(**state: object) -> JiuWenSwarmDeepAdapter:
    """Create a bare adapter with internal state set via setattr."""
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._is_session_scoped_adapter = True  # pylint: disable=protected-access
    adapter._parent_session_id = None  # pylint: disable=protected-access
    adapter._session_adapter_last_used = {}  # pylint: disable=protected-access
    for name, value in state.items():
        setattr(adapter, name, value)
    return adapter


@pytest.mark.asyncio
async def test_closing_delegated_stream_releases_session_output_lease():
    """根 adapter 在 yield 处关闭时，同步关闭内层消费流，不依赖异步 GC。"""
    from openjiuwen.harness.schema.interaction import OutputLeaseManager

    leases = OutputLeaseManager()

    async def output():
        lease = await leases.attach()
        try:
            yield "partial"
            await asyncio.Event().wait()
        finally:
            await leases.detach(lease.token)

    inner = output()
    scoped = SimpleNamespace(process_message_stream_impl=lambda *_: inner)
    root = _make_adapter(
        _is_session_scoped_adapter=False,
        _get_or_create_session_adapter=AsyncMock(return_value=scoped),
        _evict_idle_session_adapters=AsyncMock(),
    )
    outer = root.process_message_stream_impl(_build_chat_send_request({"query": "hello"}), {})
    try:
        assert await anext(outer) == "partial"
        await outer.aclose()
        assert not leases.has_consumer()
        assert await leases.attach() is not None
    finally:
        await outer.aclose()
        await inner.aclose()


@pytest.mark.asyncio
async def test_interaction_supplement_clears_pending_ask_user_state() -> None:
    """Supplement text must start a new turn, not answer the interrupted question."""
    loop_session = MagicMock()
    loop_session.get_session_id.return_value = "tui_sess_1"
    loop_session.commit = AsyncMock()
    interruption_state = _interruption_state("ask_user")
    loop_session.get_state.return_value = interruption_state
    context = MagicMock()
    context.get_messages.return_value = [
        SimpleNamespace(tool_calls=[]),
        interruption_state.ai_message,
    ]
    context_engine = MagicMock()
    context_engine.get_context.return_value = context
    context_engine.save_contexts = AsyncMock()

    instance = MagicMock()
    instance._interaction_started = True
    instance._loop_session = loop_session
    instance.react_agent = SimpleNamespace(context_engine=context_engine)
    instance.cancel_round = AsyncMock(return_value=False)

    rail = MagicMock()
    rail.get_cancelled_tool_results.return_value = []
    adapter = _make_adapter(
        _active_session_ids={},
        _stream_event_rail=rail,
        _instance=instance,
    )

    response = await adapter.process_interrupt(_build_supplement_request())

    loop_session.update_state.assert_called_once_with({INTERRUPTION_KEY: None})
    context.pop_messages.assert_called_once_with(1, with_history=True)
    context_engine.save_contexts.assert_awaited_once_with(loop_session)
    loop_session.commit.assert_awaited_once_with()
    assert response.payload["intent"] == "supplement"
    assert response.payload["new_input"] == "再执行一次"


@pytest.mark.parametrize("tool_names", [("ask_user",), ("ask_user", "confirm")])
@pytest.mark.asyncio
async def test_interaction_cancel_clears_pending_interrupt_state(
    tool_names: tuple[str, ...],
) -> None:
    """Stopping abandons the whole pending interaction, including mixed rounds."""
    loop_session = MagicMock()
    loop_session.get_session_id.return_value = "tui_sess_1"
    loop_session.commit = AsyncMock()
    interruption_state = _interruption_state(*tool_names)
    loop_session.get_state.return_value = interruption_state
    context = MagicMock()
    context.get_messages.return_value = [
        SimpleNamespace(tool_calls=[]),
        interruption_state.ai_message,
    ]
    context_engine = MagicMock()
    context_engine.get_context.return_value = context
    context_engine.save_contexts = AsyncMock()

    instance = MagicMock()
    instance._interaction_started = True
    instance._loop_session = loop_session
    instance.react_agent = SimpleNamespace(context_engine=context_engine)
    instance.goal_manager = None
    instance.cancel_round = AsyncMock(return_value=False)

    rail = MagicMock()
    rail.get_cancelled_tool_results.return_value = []
    adapter = _make_adapter(
        _active_session_ids={},
        _stream_event_rail=rail,
        _instance=instance,
    )

    response = await adapter.process_interrupt(_build_cancel_request())

    loop_session.update_state.assert_called_once_with({INTERRUPTION_KEY: None})
    context.pop_messages.assert_called_once_with(1, with_history=True)
    context_engine.save_contexts.assert_awaited_once_with(loop_session)
    loop_session.commit.assert_awaited_once_with()
    assert response.payload["intent"] == "cancel"


@pytest.mark.parametrize(
    ("session_id", "tool_names"),
    [
        ("other_session", ("ask_user",)),
        ("tui_sess_1", ("confirm",)),
        ("tui_sess_1", ("ask_user", "confirm")),
    ],
)
@pytest.mark.asyncio
async def test_supplement_keeps_unrelated_interrupt_state(
    session_id: str,
    tool_names: tuple[str, ...],
) -> None:
    """Do not clear another session or non-ask_user interaction state."""
    loop_session = MagicMock()
    loop_session.get_session_id.return_value = "tui_sess_1"
    loop_session.get_state.return_value = _interruption_state(*tool_names)
    instance = MagicMock()
    instance._loop_session = loop_session
    adapter = _make_adapter(_instance=instance)

    cleared = await getattr(
        adapter,
        "_clear_pending_interaction_interrupt",
    )(session_id)

    assert cleared is False
    loop_session.update_state.assert_not_called()


@pytest.mark.asyncio
async def test_supplement_keeps_ask_user_state_when_context_cannot_be_rolled_back() -> None:
    """Never clear state unless the matching ask_user call is the context tail."""
    interruption_state = _interruption_state("ask_user")
    loop_session = MagicMock()
    loop_session.get_session_id.return_value = "tui_sess_1"
    loop_session.get_state.return_value = interruption_state
    context = MagicMock()
    context.get_messages.return_value = [SimpleNamespace(tool_calls=[])]
    context_engine = MagicMock()
    context_engine.get_context.return_value = context
    context_engine.save_contexts = AsyncMock()
    instance = MagicMock()
    instance._loop_session = loop_session
    instance.react_agent = SimpleNamespace(context_engine=context_engine)
    adapter = _make_adapter(_instance=instance)

    cleared = await getattr(
        adapter,
        "_clear_pending_interaction_interrupt",
    )("tui_sess_1")

    assert cleared is False
    context.pop_messages.assert_not_called()
    loop_session.update_state.assert_not_called()
    context_engine.save_contexts.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_runs_teardown_when_session_not_in_active_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When session is not active, per-session teardown runs but global abort is skipped.

    Global abort (instance.abort) is unsafe when the session is inactive — a
    just-starting session on the same adapter could be killed as collateral.
    Per-session teardown (rail abort, shell kill) is sufficient for the target.
    """
    rail = MagicMock()
    rail.get_cancelled_tool_results.return_value = []
    instance = MagicMock()
    instance.abort = AsyncMock()
    # Force the non-interaction interrupt path under test (rail teardown /
    # skip global abort).  A bare MagicMock makes ``_interaction_started``
    # truthy and would divert into cancel_round().
    instance._interaction_started = False
    adapter = _make_adapter(
        _active_session_ids={},
        _session_agent_tasks={},
        _stream_event_rail=rail,
        _instance=instance,
    )

    kill_mock = MagicMock(return_value=2)
    monkeypatch.setattr(
        "openjiuwen.core.sys_operation.shell_process_registry.kill_shell_processes_for_session_tree",
        kill_mock,
    )
    monkeypatch.setattr(adapter, "_cancel_pending_todos", AsyncMock(return_value=[]))
    monkeypatch.setattr(adapter, "_cancel_scheduler_running_tasks", MagicMock())

    response = await adapter.process_interrupt(_build_cancel_request())

    # Per-session teardown must still run
    rail.abort.assert_called_once_with("tui_sess_1")
    rail.collect_cancelled_tool_updates.assert_called_once_with("tui_sess_1")
    rail.reset_for_new_task.assert_called_once_with("tui_sess_1")
    kill_mock.assert_called_once_with("tui_sess_1")
    # Global abort must NOT fire — session is inactive, could kill a just-starting session
    instance.abort.assert_not_awaited()
    assert response.payload["event_type"] == "chat.interrupt_result"
    assert response.payload["intent"] == "cancel"
    assert response.payload["success"] is True


@pytest.mark.asyncio
async def test_interaction_cancel_pauses_active_goal_before_cancel_round() -> None:
    """User stop should pause ACTIVE Goal then cancel_round; payload carries goal."""
    from openjiuwen.harness.goal.schema import GoalRecord, GoalStatus

    paused_record = GoalRecord.create(session_id="sess-goal", objective="keep going")
    paused_record.status = GoalStatus.PAUSED
    active_record = GoalRecord.create(session_id="sess-goal", objective="keep going")
    active_record.status = GoalStatus.ACTIVE

    goal_manager = MagicMock()
    goal_manager.get = AsyncMock(return_value=active_record)
    goal_manager.pause = AsyncMock(return_value=paused_record)

    instance = MagicMock()
    instance._interaction_started = True
    instance.goal_manager = goal_manager
    instance.cancel_round = AsyncMock(return_value=True)

    rail = MagicMock()
    rail.get_cancelled_tool_results.return_value = []
    adapter = _make_adapter(
        _active_session_ids={"sess-goal": 1},
        _stream_event_rail=rail,
        _instance=instance,
    )
    adapter._cancel_pending_todos = AsyncMock(return_value=None)

    response = await adapter.process_interrupt(
        AgentRequest(
            request_id="req-stop",
            channel_id="web",
            session_id="sess-goal",
            req_method=ReqMethod.CHAT_CANCEL,
            params={"intent": "cancel", "mode": "agent"},
        )
    )

    goal_manager.pause.assert_awaited_once()
    instance.cancel_round.assert_awaited_once_with(reason="user_cancel")
    rail.abort.assert_called_once_with("sess-goal")
    rail.collect_cancelled_tool_updates.assert_called_once_with("sess-goal")
    rail.reset_for_new_task.assert_called_once_with("sess-goal")
    assert response.payload["event_type"] == "chat.interrupt_result"
    assert response.payload["goal"]["status"] == "paused"
    assert response.payload["goal"]["objective"] == "keep going"


@pytest.mark.asyncio
async def test_interaction_cancel_skips_pause_when_no_goal() -> None:
    goal_manager = MagicMock()
    goal_manager.get = AsyncMock(return_value=None)
    goal_manager.pause = AsyncMock()

    instance = MagicMock()
    instance._interaction_started = True
    instance.goal_manager = goal_manager
    instance.cancel_round = AsyncMock(return_value=True)

    rail = MagicMock()
    rail.get_cancelled_tool_results.return_value = []
    adapter = _make_adapter(
        _active_session_ids={"sess-x": 1},
        _stream_event_rail=rail,
        _instance=instance,
    )
    adapter._cancel_pending_todos = AsyncMock(return_value=None)

    response = await adapter.process_interrupt(_build_cancel_request("sess-x"))

    goal_manager.pause.assert_not_awaited()
    instance.cancel_round.assert_awaited_once()
    rail.abort.assert_called_once_with("sess-x")
    assert "goal" not in response.payload


@pytest.mark.asyncio
async def test_interaction_cancel_appends_cancelled_tools_to_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Interaction cancel must close in_progress tools in history (no spinner on refresh)."""
    cancelled_tools = [
        {
            "tool_name": "task_tool",
            "tool_call_id": "call_1",
            "result": "cancelled by user",
            "status": "error",
        }
    ]
    rail = MagicMock()
    rail.get_cancelled_tool_results.return_value = cancelled_tools

    instance = MagicMock()
    instance._interaction_started = True
    instance.goal_manager = None
    instance.active_round = SimpleNamespace(work=SimpleNamespace(request_id="req-picture"))

    async def cancel_round(**_kwargs):
        # 下一轮已成为 active，工具历史仍必须关联原轮。
        instance.active_round = SimpleNamespace(work=SimpleNamespace(request_id="req-weather"))
        return True

    instance.cancel_round = AsyncMock(side_effect=cancel_round)

    adapter = _make_adapter(
        _active_session_ids={"sess-tools": 1},
        _stream_event_rail=rail,
        _instance=instance,
        _session_agent_tasks={},
    )
    adapter._cancel_pending_todos = AsyncMock(return_value=None)
    adapter._cancel_session_agent_tasks = AsyncMock(return_value=0)

    append_mock = MagicMock()
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep.append_history_record",
        append_mock,
    )

    response = await adapter.process_interrupt(_build_cancel_request("sess-tools"))

    # Must not cancel stream producer tasks on interaction path
    adapter._cancel_session_agent_tasks.assert_not_awaited()
    instance.cancel_round.assert_awaited_once_with(reason="user_cancel")
    rail.abort.assert_called_once_with("sess-tools")
    assert response.payload["cancelled_tools"] == cancelled_tools
    append_mock.assert_called_once()
    assert append_mock.call_args.kwargs["event_type"] == "chat.tool_result"
    assert append_mock.call_args.kwargs["extra"]["tool_result"]["tool_call_id"] == "call_1"
    assert append_mock.call_args.kwargs["request_id"] == "req-picture"
    assert append_mock.call_args.kwargs["content"] == ""


@pytest.mark.asyncio
async def test_cancel_history_waits_for_pending_tool_call_write(tmp_path, monkeypatch):
    """active_round 已清除时，仍按尚未写盘的 call_id 归属原轮。"""
    import asyncio
    import threading
    from jiuwenswarm.server.runtime.session import session_history

    sid = "sess-pending-cancel"
    started, release = threading.Event(), threading.Event()
    write = session_history._write_item

    def delayed_write(session_id, item):
        if session_id == sid and item.get("event_type") == "chat.tool_call":
            started.set()
            release.wait(timeout=5)
        write(session_id, item)

    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)
    monkeypatch.setattr(session_history, "_write_item", delayed_write)
    session_history.append_history_record(
        session_id=sid, request_id="req-picture", channel_id="tui", role="assistant",
        event_type="chat.tool_call", content="", timestamp=1,
        extra={"tool_call": {"name": "bash", "tool_call_id": "picture-call"}},
    )
    await asyncio.to_thread(started.wait, 5)
    assert started.is_set()
    rail = MagicMock()
    rail.get_cancelled_tool_results.return_value = [{
        "tool_name": "bash", "tool_call_id": "picture-call", "result": "cancelled", "status": "error"
    }]
    instance = MagicMock()
    instance._interaction_started = True
    instance.active_round = None
    instance.goal_manager = None
    instance.cancel_round = AsyncMock(return_value=True)
    adapter = _make_adapter(_instance=instance, _stream_event_rail=rail, _active_session_ids={sid: 1})
    adapter._cancel_pending_todos = AsyncMock(return_value=None)
    asyncio.get_running_loop().call_later(0.05, release.set)
    try:
        await adapter.process_interrupt(_build_cancel_request(sid))
    finally:
        release.set()
        await asyncio.to_thread(session_history.flush_history_writes)
    results = [r for r in session_history.load_history_records(sid) if r.get("event_type") == "chat.tool_result"]
    assert len(results) == 1
    assert results[0]["request_id"] == "req-picture"
    assert results[0]["content"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_io", ["read", "write"])
async def test_history_io_failure_does_not_replace_successful_cancel(monkeypatch, failed_io):
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    def fail(*_args, **_kwargs):
        raise OSError("history disk unavailable")

    rail = MagicMock()
    rail.get_cancelled_tool_results.return_value = [{
        "tool_name": "bash", "tool_call_id": "picture-call", "result": "cancelled", "status": "error"
    }]
    instance = MagicMock()
    instance._interaction_started = True
    instance.goal_manager = None
    instance.active_round = SimpleNamespace(work=SimpleNamespace(request_id="req-picture"))
    instance.cancel_round = AsyncMock(return_value=True)
    adapter = _make_adapter(_instance=instance, _stream_event_rail=rail, _active_session_ids={"sess-io": 1})
    adapter._cancel_pending_todos = AsyncMock(return_value=None)
    monkeypatch.setattr(interface_deep, "load_history_records", fail if failed_io == "read" else lambda *_: [])
    monkeypatch.setattr(interface_deep, "append_history_record", fail if failed_io == "write" else lambda **_: None)
    response = await adapter.process_interrupt(_build_cancel_request("sess-io"))
    assert response.ok is True
    assert response.payload["success"] is True
    assert response.payload["cancelled_tools"][0]["tool_call_id"] == "picture-call"


@pytest.mark.asyncio
async def test_unmark_skips_rail_cleanup_when_stream_consumer_cancelled() -> None:
    rail = MagicMock()
    adapter = _make_adapter(
        _active_session_ids={"sess_a": 1},
        _stream_event_rail=rail,
    )

    getattr(adapter, "_unmark_session_active")("sess_a", cleanup_rail=False)

    rail.cleanup_session.assert_not_called()
    assert "sess_a" not in getattr(adapter, "_active_session_ids")


@pytest.mark.asyncio
async def test_unmark_cleans_rail_on_normal_completion() -> None:
    rail = MagicMock()
    adapter = _make_adapter(
        _active_session_ids={"sess_a": 1},
        _stream_event_rail=rail,
    )

    getattr(adapter, "_unmark_session_active")("sess_a")

    rail.cleanup_session.assert_called_once_with("sess_a")
    assert "sess_a" not in getattr(adapter, "_active_session_ids")


@pytest.mark.asyncio
async def test_abort_skipped_when_other_sessions_active_even_if_target_executing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """instance.abort() is global on the shared DeepAgent — when other sessions are
    active, it must NEVER be called, even if the target session is executing.
    Per-session teardown (rail abort, task cancel, shell kill) is sufficient.
    """
    rail = MagicMock()
    rail.get_cancelled_tool_results.return_value = []
    instance = MagicMock()
    setattr(instance, "abort", AsyncMock())
    setattr(instance, "_interaction_started", False)
    setattr(instance, "_invoke_active", True)
    stream_task = MagicMock()
    stream_task.done.return_value = False
    setattr(instance, "_stream_process_task", stream_task)
    loop_session = MagicMock()
    loop_session.get_session_id.return_value = "tui_target"
    setattr(instance, "_loop_session", loop_session)
    adapter = _make_adapter(
        _active_session_ids={"tui_other": 1},
        _session_agent_tasks={},
        _stream_event_rail=rail,
        _instance=instance,
    )

    monkeypatch.setattr(
        "openjiuwen.core.sys_operation.shell_process_registry.kill_shell_processes_for_session_tree",
        MagicMock(return_value=0),
    )
    monkeypatch.setattr(adapter, "_cancel_pending_todos", AsyncMock(return_value=[]))
    monkeypatch.setattr(adapter, "_cancel_scheduler_running_tasks", MagicMock())

    await adapter.process_interrupt(_build_cancel_request(session_id="tui_target"))

    # instance.abort must NOT be called — it would kill tui_other's work too
    instance.abort.assert_not_awaited()
    # But per-session teardown must still run
    rail.abort.assert_called_once_with("tui_target")


@pytest.mark.asyncio
async def test_stream_fresh_turn_drops_stale_interrupt_before_dispatch() -> None:
    """串起真实入口：chat.send 普通消息进来时，先清残留中断再走后续分发。"""
    interruption_state = _interruption_state("task_tool")
    loop_session = MagicMock()
    loop_session.get_session_id.return_value = "desktop_sess_1"
    loop_session.commit = AsyncMock()
    loop_session.get_state.return_value = interruption_state
    context = MagicMock()
    context.get_messages.return_value = [SimpleNamespace(tool_calls=[])]
    context_engine = MagicMock()
    context_engine.get_context.return_value = context
    context_engine.save_contexts = AsyncMock()
    instance = MagicMock()
    instance._loop_session = loop_session
    instance.react_agent = SimpleNamespace(context_engine=context_engine)
    adapter = _make_adapter(_instance=instance)
    adapter._bind_invoke_workspace_context = MagicMock()  # pylint: disable=protected-access
    adapter._has_valid_model_config = MagicMock(return_value=False)  # pylint: disable=protected-access

    request = _build_chat_send_request(
        {"query": "还没好吗", "mode": "agent.plan"},
        session_id="desktop_sess_1",
    )
    chunks = [
        chunk
        async for chunk in adapter.process_message_stream_impl(
            request, {"query": "还没好吗", "conversation_id": "desktop_sess_1"}
        )
    ]

    # 未配模型时会在分发前返回 chat.error；关键是残留中断状态已被清掉。
    loop_session.update_state.assert_called_once_with({INTERRUPTION_KEY: None})
    context_engine.save_contexts.assert_awaited_once_with(loop_session)
    assert [chunk.payload.get("event_type") for chunk in chunks] == ["chat.error"]


@pytest.mark.asyncio
async def test_stream_hitl_answer_keeps_pending_interrupt_state() -> None:
    """带 answers 的 chat.send 是作答，不能清掉正在等的交互状态。"""
    interruption_state = _interruption_state("ask_user")
    loop_session = MagicMock()
    loop_session.get_session_id.return_value = "desktop_sess_1"
    loop_session.get_state.return_value = interruption_state
    context_engine = MagicMock()
    instance = MagicMock()
    instance._loop_session = loop_session
    instance.react_agent = SimpleNamespace(context_engine=context_engine)
    adapter = _make_adapter(_instance=instance)
    adapter._bind_invoke_workspace_context = MagicMock()  # pylint: disable=protected-access
    adapter._has_valid_model_config = MagicMock(return_value=False)  # pylint: disable=protected-access

    request = _build_chat_send_request(
        {
            "query": "",
            "request_id": "call-ask",
            "source": "ask_user_interrupt",
            "answers": [{"question": "继续吗？", "selected_options": ["继续"]}],
            "mode": "agent.plan",
        },
        session_id="desktop_sess_1",
    )
    [
        chunk
        async for chunk in adapter.process_message_stream_impl(
            request, {"query": "", "conversation_id": "desktop_sess_1"}
        )
    ]

    loop_session.update_state.assert_not_called()


@pytest.mark.asyncio
async def test_fresh_turn_drops_stale_task_tool_interrupt_without_context_tail() -> None:
    """新一轮消息必须丢弃残留的 task_tool（子代理）中断，而不是被它吞掉。

    真实故障（2026-09-21）：并行子代理里 PPT 子代理永久挂起后，
    ``interrupted_tools`` 里留下 task_tool 条目，且父会话上下文尾部是其它
    子代理的 tool 结果（签名对不上）。旧逻辑只在「尾部正好是被中断的
    ai_message」时才清理，于是残留状态一直在，之后每条普通消息都被
    ``handle_resume`` 当成对旧中断的回答消费 → 不管说什么都弹「询问你」。
    """
    interruption_state = _interruption_state("task_tool")
    loop_session = MagicMock()
    loop_session.get_session_id.return_value = "tui_sess_1"
    loop_session.commit = AsyncMock()
    loop_session.get_state.return_value = interruption_state
    context = MagicMock()
    context.get_messages.return_value = [
        SimpleNamespace(tool_calls=[]),
        SimpleNamespace(tool_calls=[SimpleNamespace(id="call-other", name="task_tool")]),
    ]
    context_engine = MagicMock()
    context_engine.get_context.return_value = context
    context_engine.save_contexts = AsyncMock()
    instance = MagicMock()
    instance._loop_session = loop_session
    instance.react_agent = SimpleNamespace(context_engine=context_engine)
    adapter = _make_adapter(_instance=instance)

    await adapter._discard_stale_interrupt_for_fresh_turn(
        _build_chat_send_request({"query": "还没好吗", "mode": "agent.plan"})
    )

    loop_session.update_state.assert_called_once_with({INTERRUPTION_KEY: None})
    context.pop_messages.assert_not_called()
    context_engine.save_contexts.assert_awaited_once_with(loop_session)
    loop_session.commit.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_non_forced_clear_still_keeps_state_without_matching_tail() -> None:
    """非 force 的调用方（stop/supplement 的纯 ask_user 清理）行为保持不变。"""
    interruption_state = _interruption_state("ask_user")
    loop_session = MagicMock()
    loop_session.get_session_id.return_value = "tui_sess_1"
    loop_session.get_state.return_value = interruption_state
    context = MagicMock()
    context.get_messages.return_value = [SimpleNamespace(tool_calls=[])]
    context_engine = MagicMock()
    context_engine.get_context.return_value = context
    context_engine.save_contexts = AsyncMock()
    instance = MagicMock()
    instance._loop_session = loop_session
    instance.react_agent = SimpleNamespace(context_engine=context_engine)
    adapter = _make_adapter(_instance=instance)

    cleared = await getattr(adapter, "_clear_pending_interaction_interrupt")("tui_sess_1")

    assert cleared is False
    loop_session.update_state.assert_not_called()


def test_is_fresh_turn_request_distinguishes_hitl_answers() -> None:
    """只有带非空 query 的普通消息算新一轮；HITL 作答与 steer 不算。"""
    adapter = _make_adapter()
    is_fresh = getattr(adapter, "_is_fresh_turn_request")

    assert is_fresh({"query": "还没好吗", "mode": "agent.plan"}) is True
    assert is_fresh({"query": "补充一句", "input_mode": "steer"}) is False
    assert is_fresh({"query": "", "input_mode": "follow_up"}) is False
    assert (
        is_fresh(
            {
                "query": "",
                "request_id": "call_1",
                "source": "ask_user_interrupt",
                "answers": [{"question": "继续吗？", "selected_options": ["继续"]}],
            }
        )
        is False
    )
    assert is_fresh({"query": "   "}) is False
    assert is_fresh(None) is False


def test_reset_runtime_cron_context_resets_shell_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep
    from openjiuwen.core.sys_operation.shell_process_registry import (
        set_shell_session_id,
    )

    reset_shell_mock = MagicMock()
    monkeypatch.setattr(
        "openjiuwen.core.sys_operation.shell_process_registry.reset_shell_session_id",
        reset_shell_mock,
    )
    for var_name in (
        "_CRON_TOOL_BOUND",
        "_CRON_TOOL_MODE",
        "_CRON_TOOL_METADATA",
        "_CRON_TOOL_SESSION_ID",
        "_CRON_TOOL_CHANNEL_ID",
    ):
        monkeypatch.setattr(
            f"jiuwenswarm.server.runtime.agent_adapter.interface_deep.{var_name}",
            MagicMock(),
        )

    shell_token = set_shell_session_id("sess_reset")
    getattr(JiuWenSwarmDeepAdapter, "_reset_runtime_cron_context")(
        interface_deep._RuntimeCronContextTokens(
            channel=MagicMock(),
            session=MagicMock(),
            metadata=MagicMock(),
            mode=MagicMock(),
            bound=MagicMock(),
            shell=shell_token,
        )
    )
    reset_shell_mock.assert_called_once_with(shell_token)


def test_bind_runtime_cron_context_fills_locked_session_project_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda session_id, cache_bust=False: {
            "session_id": session_id,
            "project_id": "proj_locked",
            "project_dir": "D:\\locked-project",
            "work_mode": "code",
        },
    )

    tokens = JiuWenSwarmDeepAdapter._bind_runtime_cron_context(
        channel_id="web",
        session_id="sess_locked",
        metadata={"request_id": "req-old"},
        request_id="req-new",
        mode="agent",
        project_dir=None,
    )
    try:
        metadata = interface_deep._CRON_TOOL_METADATA.get()
        assert metadata["request_id"] == "req-new"
        assert metadata["project_id"] == "proj_locked"
        assert metadata["project_dir"] == "D:\\locked-project"
        assert metadata["work_mode"] == "code"
    finally:
        JiuWenSwarmDeepAdapter._reset_runtime_cron_context(tokens)


def test_runtime_cron_tool_context_falls_back_to_last_bound_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda session_id, cache_bust=False: {
            "session_id": session_id,
            "project_id": "proj_runtime",
            "project_dir": "D:\\runtime-project",
            "work_mode": "work",
        },
    )

    context = interface_deep._RuntimeCronToolContext(tool_scope="runtime_test")
    tokens = JiuWenSwarmDeepAdapter._bind_runtime_cron_context(
        channel_id="web",
        session_id="sess_runtime",
        metadata={},
        request_id="req-runtime",
        mode="agent",
        project_dir=None,
    )
    try:
        context.remember_current_binding()
    finally:
        JiuWenSwarmDeepAdapter._reset_runtime_cron_context(tokens)

    assert context.session_id == "sess_runtime"
    assert context.mode == "agent"
    metadata = context.metadata
    assert metadata["request_id"] == "req-runtime"
    assert metadata["project_id"] == "proj_runtime"
    assert metadata["project_dir"] == "D:\\runtime-project"
    assert metadata["work_mode"] == "work"
