# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for JiuWenSwarmDeepAdapter interrupt when stream consumer already unwound."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from openjiuwen.harness.schema.task import TodoItem, TodoStatus

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
    for name, value in state.items():
        setattr(adapter, name, value)
    return adapter


@pytest.mark.asyncio
async def test_cancel_pending_todos_uses_public_tool_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancel unfinished todos through TodoModifyTool.invoke, not its internals."""
    from jiuwenswarm.agents.harness.common.tools.todo_compat import (
        CompatibleTodoModifyTool,
    )

    todos = [
        TodoItem(id="pending", status=TodoStatus.PENDING),
        TodoItem(id="running", status=TodoStatus.IN_PROGRESS),
        TodoItem(id="done", status=TodoStatus.COMPLETED),
    ]
    todo_tool = CompatibleTodoModifyTool(operation=MagicMock())
    todo_tool.load_todos = AsyncMock(return_value=todos)
    todo_tool.save_todos = AsyncMock()
    todo_tool.invoke = AsyncMock(wraps=todo_tool.invoke)

    resource_mgr = MagicMock()
    resource_mgr.get_tool.return_value = todo_tool
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep.Runner.resource_mgr",
        resource_mgr,
    )

    ability_manager = MagicMock()
    ability_manager.get.return_value = MagicMock(id="todo_modify")
    instance = MagicMock(ability_manager=ability_manager, card=None)
    formatted_todos = [{"id": "pending", "status": "cancelled"}]
    rail = MagicMock()
    rail._format_todos_for_frontend.return_value = formatted_todos
    adapter = _make_adapter(_instance=instance, _stream_event_rail=rail)

    result = await adapter._cancel_pending_todos("session-1")

    todo_tool.invoke.assert_awaited_once()
    invoke_args, invoke_kwargs = todo_tool.invoke.await_args
    assert invoke_args == ({"action": "cancel", "ids": ["pending", "running"]},)
    assert invoke_kwargs["session"].get_session_id() == "session-1"
    todo_tool.save_todos.assert_awaited_once_with("session-1", todos)
    assert [todo.status for todo in todos] == [
        TodoStatus.CANCELLED,
        TodoStatus.CANCELLED,
        TodoStatus.COMPLETED,
    ]
    rail._format_todos_for_frontend.assert_called_once_with(todos)
    assert result == formatted_todos


@pytest.mark.asyncio
async def test_interaction_supplement_clears_pending_ask_user_state() -> None:
    """Supplement text must start a new turn, not answer the interrupted question."""
    loop_session = MagicMock()
    loop_session.get_session_id.return_value = "tui_sess_1"
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
    assert response.payload["intent"] == "supplement"
    assert response.payload["new_input"] == "再执行一次"


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
        "_clear_pending_ask_user_interrupt_for_supplement",
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
        "_clear_pending_ask_user_interrupt_for_supplement",
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
async def test_interaction_cancel_stops_scheduler_task_before_cancel_round() -> None:
    """A synchronous task_tool subagent must not block the interrupt response."""
    child_started = asyncio.Event()
    child_cancelled = asyncio.Event()

    async def _run_child() -> None:
        child_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            child_cancelled.set()
            raise

    child_task = asyncio.create_task(_run_child())
    await child_started.wait()

    scheduler = SimpleNamespace(_running_tasks={"round-task": (None, child_task)})
    instance = MagicMock()
    instance._interaction_started = True
    instance._loop_controller = SimpleNamespace(_task_scheduler=scheduler)
    instance.goal_manager = None

    async def _cancel_round(*, reason: str) -> bool:
        assert reason == "user_cancel"
        await asyncio.wait_for(child_cancelled.wait(), timeout=0.2)
        return True

    instance.cancel_round = AsyncMock(side_effect=_cancel_round)

    rail = MagicMock()
    rail.get_cancelled_tool_results.return_value = []
    adapter = _make_adapter(
        _active_session_ids={"sess-task-tool": 1},
        _stream_event_rail=rail,
        _instance=instance,
    )
    adapter._cancel_pending_todos = AsyncMock(return_value=None)
    adapter._cancel_session_agent_tasks = AsyncMock(return_value=0)

    try:
        response = await asyncio.wait_for(
            adapter.process_interrupt(_build_cancel_request("sess-task-tool")),
            timeout=0.5,
        )
    finally:
        if not child_task.done():
            child_task.cancel()
        await asyncio.gather(child_task, return_exceptions=True)

    assert child_task.cancelled()
    adapter._cancel_session_agent_tasks.assert_not_awaited()
    instance.cancel_round.assert_awaited_once_with(reason="user_cancel")
    assert response.payload["event_type"] == "chat.interrupt_result"
    assert response.payload["success"] is True


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
    instance.cancel_round = AsyncMock(return_value=True)

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
        "_CRON_TOOL_USER_ID",
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
            user_id=MagicMock(),
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
