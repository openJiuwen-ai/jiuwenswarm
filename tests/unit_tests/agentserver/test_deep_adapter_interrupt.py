# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for JiuWenSwarmDeepAdapter interrupt when stream consumer already unwound."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.core.single_agent.interrupt.response import InterruptRequest
from openjiuwen.harness.schema.task import TodoItem, TodoStatus

from openjiuwen.core.single_agent.interrupt.state import INTERRUPTION_KEY
from openjiuwen.harness.schema.interaction import SendInputRequest
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue import (
    RootPermissionQueue,
    RootPermissionQueueError,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_ask_user import (
    ASK_USER_CONTINUATION_METADATA_KEY,
    ASK_USER_RESUME_DTO_KEY,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_context import (
    ROOT_CONTEXT_KEY,
    RootDecisionContext,
    RootIntentTurn,
    RootIntentTurnKind,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue_rail import (
    ROOT_NON_PERMISSION_RESUME_DTO_KEY,
    RootNonPermissionResume,
)
from jiuwenswarm.server.runtime.agent_adapter.permission_dispatch import (
    RootPermissionDispatch, RootPermissionDispatchHandoff,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)


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
    adapter._enable_auto_permission = False  # pylint: disable=protected-access
    adapter._root_permission_queue = state.pop(
        "_root_permission_queue", RootPermissionQueue()
    )
    adapter._permission_dispatch = RootPermissionDispatch(adapter._root_permission_queue)
    adapter._permission_dispatch.lock = state.pop("dispatch_lock", adapter._permission_dispatch.lock)
    adapter._permission_dispatch.handoff = state.pop("handoff", None)
    for name, value in state.items():
        setattr(adapter, name, value)
    return adapter


def _make_smart_adapter(**state: object) -> JiuWenSwarmDeepAdapter:
    """Enable Smart only for fixtures exercising its queue and continuation."""
    return _make_adapter(_enable_auto_permission=True, **state)


def _begin_invocation(
    queue: RootPermissionQueue,
    *,
    session_id: str,
    request_id: str,
    tool_call_id: str,
):
    return queue.begin(
        root_session_id=session_id,
        request_id=request_id,
        execution_session_id=session_id,
        tool_call_id=tool_call_id,
        tool_name="bash",
    )


def _pending_permission_state(queue: RootPermissionQueue, *, session_id: str):
    card = _begin_invocation(
        queue,
        session_id=session_id,
        request_id="request-old",
        tool_call_id="call-old",
    )
    request = InterruptRequest(
        message="approve",
        metadata={"tool_invocation_key": card.key.to_wire()},
    )
    queue.mark_pending(
        card.key,
        request=request,
        auto_manual=True,
        root_context=None,
    )
    tool_call = SimpleNamespace(id=card.key.tool_call_id, name="bash")
    return card, SimpleNamespace(
        ai_message=SimpleNamespace(tool_calls=[tool_call]),
        interrupted_tools={
            tool_call.id: SimpleNamespace(
                tool_call=tool_call,
                interrupt_requests={card.key.tool_call_id: request},
            )
        },
    )


def _permission_core_state(session_id: str, interruption_state: object):
    loop_session = MagicMock()
    loop_session.get_session_id.return_value = session_id
    loop_session.get_state.return_value = interruption_state
    context = MagicMock()
    context.get_messages.return_value = [interruption_state.ai_message]
    context.add_messages = AsyncMock()
    context_engine = MagicMock()
    context_engine.get_context.return_value = context
    context_engine.save_contexts = AsyncMock()
    return loop_session, context, context_engine


def _nonpermission_core_state(
    session_id: str,
    *,
    tool_name: str,
    tool_call_id: str = "call-control",
    metadata: dict | None = None,
) -> SimpleNamespace:
    tool_call = SimpleNamespace(id=tool_call_id, name=tool_name)
    request = InterruptRequest(metadata=metadata or {})
    state = SimpleNamespace(
        interrupted_tools={
            tool_call_id: SimpleNamespace(
                tool_call=tool_call,
                interrupt_requests={tool_call_id: request},
            )
        }
    )
    return SimpleNamespace(
        get_session_id=MagicMock(return_value=session_id),
        get_state=MagicMock(return_value=state),
    )


def test_nonpermission_resume_dispatch_stamps_generic_typed_marker() -> None:
    tool_name = "exit_plan_mode"
    session_id = "session-control"
    loop_session = _nonpermission_core_state(session_id, tool_name=tool_name)
    adapter = _make_adapter(
        _instance=SimpleNamespace(loop_session=loop_session),
    )
    incoming = InteractiveInput()
    incoming.update(
        "call-control",
        {"approved": True, "auto_confirm": True},
    )
    request = AgentRequest(
        request_id="request-control",
        channel_id="web",
        session_id=session_id,
        req_method=ReqMethod.CHAT_SEND,
        params={"mode": "agent", "source": "confirm_interrupt"},
    )

    prepared = adapter._prepare_permission_resume_dispatch(
        request,
        {"query": incoming},
    )

    assert prepared is not None
    marker = prepared["run"]["context"]["extra"][ROOT_NON_PERMISSION_RESUME_DTO_KEY]
    assert marker == RootNonPermissionResume(
        session_id,
        "call-control",
        tool_name,
    )
    assert prepared["query"] is incoming


def test_ask_resume_keeps_generic_routing_separate_from_intent_payload() -> None:
    session_id = "session-ask"
    tool_call_id = "call-ask"
    root_context = RootDecisionContext(
        session_id=session_id,
        request_id="request-original",
        channel_id="web",
        trusted_turns=(),
    )
    loop_session = _nonpermission_core_state(
        session_id,
        tool_name="ask_user",
        tool_call_id=tool_call_id,
        metadata={
            ASK_USER_CONTINUATION_METADATA_KEY: {
                "tool_call_id": tool_call_id,
                "context": root_context.to_mapping(),
                "questions": [
                    {
                        "question": "Which project?",
                        "options": [],
                        "multi_select": False,
                    }
                ],
            }
        },
    )
    adapter = _make_adapter(
        _instance=SimpleNamespace(loop_session=loop_session),
    )
    incoming = InteractiveInput()
    incoming.update(
        tool_call_id,
        {"answers": {"Which project?": "JiuwenSwarm"}},
    )
    request = AgentRequest(
        request_id="request-answer",
        channel_id="web",
        session_id=session_id,
        req_method=ReqMethod.CHAT_SEND,
        params={"mode": "agent", "source": "ask_user_interrupt"},
    )

    prepared = adapter._prepare_permission_resume_dispatch(
        request,
        {"query": incoming},
    )

    extra = prepared["run"]["context"]["extra"]
    assert extra[ROOT_NON_PERMISSION_RESUME_DTO_KEY] == RootNonPermissionResume(
        session_id,
        tool_call_id,
        "ask_user",
    )
    assert extra[ASK_USER_RESUME_DTO_KEY].clarifications[0].answers == ("JiuwenSwarm",)


def test_nonpermission_resume_dispatch_rejects_live_permission_scope() -> None:
    session_id = "session-conflict"
    queue = RootPermissionQueue()
    _begin_invocation(
        queue,
        session_id=session_id,
        request_id="request-active",
        tool_call_id="call-active",
    )
    loop_session = _nonpermission_core_state(
        session_id,
        tool_name="exit_plan_mode",
    )
    adapter = _make_smart_adapter(
        _instance=SimpleNamespace(loop_session=loop_session),
        _root_permission_queue=queue,
    )
    incoming = InteractiveInput()
    incoming.update("call-control", {"approved": True})
    request = AgentRequest(
        request_id="request-control",
        channel_id="web",
        session_id=session_id,
        req_method=ReqMethod.CHAT_SEND,
        params={"mode": "agent", "source": "confirm_interrupt"},
    )

    with pytest.raises(
        RootPermissionQueueError,
        match="nonpermission_resume_permission_conflict",
    ):
        adapter._prepare_permission_resume_dispatch(request, {"query": incoming})


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
async def test_unconfirmed_interaction_cancel_quarantines_inflight_permission() -> None:
    queue = RootPermissionQueue(id_factory=lambda: "tiv-inflight")
    card = _begin_invocation(
        queue,
        session_id="sess-unknown-cancel",
        request_id="request-old",
        tool_call_id="call-old",
    )
    pending = queue.mark_pending(
        card.key,
        request=InterruptRequest(
            message="approve",
            metadata={"tool_invocation_key": card.key.to_wire()},
        ),
        auto_manual=True,
        root_context=None,
    )
    queue.reconcile(
        {
            "result_type": "interrupt",
            "interrupt_ids": ["call-old"],
            "state": [{"id": "call-old", "value": pending.request}],
        },
        root_session_id="sess-unknown-cancel",
    )
    answer = InteractiveInput()
    answer.update(
        card.key.invocation_id,
        {
            "approved": True,
            "auto_confirm": False,
            "feedback": "",
        },
    )
    queue.reserve_answer("sess-unknown-cancel", answer)

    instance = MagicMock()
    instance._interaction_started = True
    instance.goal_manager = None
    instance.cancel_round = AsyncMock(return_value=False)
    rail = MagicMock()
    rail.get_cancelled_tool_results.return_value = []
    adapter = _make_smart_adapter(
        _active_session_ids={"sess-unknown-cancel": 1},
        _stream_event_rail=rail,
        _instance=instance,
        _root_permission_queue=queue,
    )

    await adapter.process_interrupt(_build_cancel_request("sess-unknown-cancel"))

    assert queue.get(card.key).state == "resuming"
    with pytest.raises(RootPermissionQueueError, match="permission_queue_quarantined"):
        queue.raise_if_quarantined("sess-unknown-cancel")


@pytest.mark.asyncio
@pytest.mark.parametrize("nested", [False, True])
async def test_interaction_cancel_discards_exact_permission_continuation(nested) -> None:
    queue = RootPermissionQueue(id_factory=lambda: "tiv-discard")
    card, state = _pending_permission_state(queue, session_id="sess-discard")
    if nested:
        entry = state.interrupted_tools.pop("call-old")
        entry.tool_call.id, entry.tool_call.name = "outer-call", "tool_call"
        state.interrupted_tools["outer-call"] = entry
    loop_session, context, context_engine = _permission_core_state(
        "sess-discard", state
    )
    instance = MagicMock()
    instance._interaction_started = True
    instance._loop_session = loop_session
    instance.react_agent = SimpleNamespace(context_engine=context_engine)
    instance.goal_manager = None
    instance.cancel_round = AsyncMock(return_value=False)
    rail = MagicMock()
    rail.get_cancelled_tool_results.return_value = []
    adapter = _make_smart_adapter(
        _active_session_ids={"sess-discard": 1},
        _stream_event_rail=rail,
        _instance=instance,
        _root_permission_queue=queue,
    )

    response = await adapter.process_interrupt(_build_cancel_request("sess-discard"))

    assert response.ok is True
    assert response.payload["success"] is True
    assert queue.get(card.key) is None
    queue.raise_if_quarantined("sess-discard")
    added_message = context.add_messages.await_args.args[0]
    assert added_message.tool_call_id == ("outer-call" if nested else "call-old")
    assert "Superseded by new user input" in added_message.content
    loop_session.update_state.assert_called_once_with({INTERRUPTION_KEY: None})
    context_engine.save_contexts.assert_awaited_once_with(loop_session)


@pytest.mark.asyncio
async def test_fresh_input_discards_exact_old_permission_before_admission() -> None:
    queue = RootPermissionQueue(id_factory=lambda: "tiv-fresh")
    card, state = _pending_permission_state(queue, session_id="sess-fresh")
    loop_session, _context, context_engine = _permission_core_state("sess-fresh", state)
    instance = MagicMock()
    instance._loop_session = loop_session
    instance.react_agent = SimpleNamespace(context_engine=context_engine)
    instance.cancel_round = AsyncMock(return_value=False)
    adapter = _make_smart_adapter(_instance=instance, _root_permission_queue=queue)
    request = AgentRequest(
        request_id="request-new",
        channel_id="web",
        session_id="sess-fresh",
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "继续执行", "mode": "agent"},
    )

    prepared = await adapter._prepare_root_input_dispatch(
        request,
        {"query": "继续执行"},
    )

    assert prepared is not None
    assert prepared["query"] == "继续执行"
    adapter._permission_dispatch.release(prepared)
    instance.cancel_round.assert_awaited_once_with(reason="fresh_user_input")
    assert queue.get(card.key) is None
    queue.raise_if_quarantined("sess-fresh")


@pytest.mark.asyncio
async def test_fresh_cutover_consumes_admitted_answer_before_late_callback() -> None:
    queue = RootPermissionQueue(id_factory=lambda: "tiv-race")
    card, state = _pending_permission_state(queue, session_id="sess-race")
    pending = queue.get(card.key)
    assert pending is not None
    queue.reconcile(
        {
            "result_type": "interrupt",
            "interrupt_ids": [pending.key.tool_call_id],
            "state": [{"id": pending.key.tool_call_id, "value": pending.request}],
        },
        root_session_id="sess-race",
    )
    incoming = InteractiveInput()
    incoming.update(
        pending.key.invocation_id,
        {"approved": True, "auto_confirm": False, "feedback": ""},
    )
    answer = queue.reserve_answer("sess-race", incoming)
    loop_session, _context, context_engine = _permission_core_state("sess-race", state)
    core_cleared = asyncio.Event()
    allow_cancel_return = asyncio.Event()

    async def cancel_round(*, reason: str) -> bool:
        assert reason == "fresh_user_input"
        loop_session.get_state.return_value = None
        core_cleared.set()
        await allow_cancel_return.wait()
        return True

    instance = MagicMock()
    instance._loop_session = loop_session
    instance.react_agent = SimpleNamespace(context_engine=context_engine)
    instance.cancel_round = cancel_round
    dispatch_lock = asyncio.Lock()
    old_handoff = RootPermissionDispatchHandoff(
        lock=dispatch_lock,
        root_session_id="sess-race",
        answer=answer,
        accepted=True,
    )
    adapter = _make_smart_adapter(
        _instance=instance,
        _root_permission_queue=queue,
        dispatch_lock=dispatch_lock,
        handoff=old_handoff,
    )
    request = AgentRequest(
        request_id="request-new",
        channel_id="web",
        session_id="sess-race",
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "开始新任务", "mode": "agent"},
    )

    fresh_task = asyncio.create_task(
        adapter._prepare_root_input_dispatch(request, {"query": "开始新任务"})
    )
    await core_cleared.wait()

    assert dispatch_lock.locked() is True
    assert queue.get(answer.card.key).state == "resuming"
    allow_cancel_return.set()
    prepared = await fresh_task

    assert queue.get(answer.card.key) is None
    with pytest.raises(
        RootPermissionQueueError,
        match="permission_queue_answer_not_reserved",
    ):
        queue.claim_answer_for_call(
            root_session_id="sess-race",
            execution_session_id="sess-race",
            tool_call_id=pending.key.tool_call_id,
            tool_name="bash",
        )
    adapter._permission_dispatch.release(prepared)


@pytest.mark.asyncio
async def test_two_fresh_dispatches_serialize_only_until_handoff_established() -> None:
    instance = MagicMock()
    instance.cancel_round = AsyncMock(return_value=False)
    adapter = _make_smart_adapter(_instance=instance)
    first_request = AgentRequest(
        request_id="request-first",
        channel_id="web",
        session_id="sess-serialized",
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "first", "mode": "agent"},
    )
    second_request = AgentRequest(
        request_id="request-second",
        channel_id="web",
        session_id="sess-serialized",
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "second", "mode": "agent"},
    )

    first = await adapter._prepare_root_input_dispatch(
        first_request,
        {"query": "first"},
    )
    second_started = asyncio.Event()

    async def prepare_second():
        second_started.set()
        return await adapter._prepare_root_input_dispatch(
            second_request,
            {"query": "second"},
        )

    second_task = asyncio.create_task(prepare_second())
    await second_started.wait()

    assert second_task.done() is False
    adapter._permission_dispatch.release(first)
    second = await second_task

    assert second["query"] == "second"
    adapter._permission_dispatch.release(second)


@pytest.mark.asyncio
async def test_permission_resume_waits_for_fresh_cutover_mutex() -> None:
    queue = RootPermissionQueue(id_factory=lambda: "tiv-fresh-resume")
    card, state = _pending_permission_state(queue, session_id="sess-fresh-resume")
    pending = queue.get(card.key)
    assert pending is not None
    queue.reconcile(
        {
            "result_type": "interrupt",
            "interrupt_ids": [pending.key.tool_call_id],
            "state": [{"id": pending.key.tool_call_id, "value": pending.request}],
        },
        root_session_id="sess-fresh-resume",
    )
    loop_session, _context, context_engine = _permission_core_state(
        "sess-fresh-resume",
        state,
    )
    cutover_entered = asyncio.Event()
    allow_cutover = asyncio.Event()

    async def cancel_round(*, reason: str) -> bool:
        assert reason == "fresh_user_input"
        cutover_entered.set()
        await allow_cutover.wait()
        return True

    instance = MagicMock()
    instance.loop_session = loop_session
    instance._loop_session = loop_session
    instance.react_agent = SimpleNamespace(context_engine=context_engine)
    instance.cancel_round = cancel_round
    adapter = _make_smart_adapter(_instance=instance, _root_permission_queue=queue)
    fresh_request = AgentRequest(
        request_id="request-fresh",
        channel_id="web",
        session_id="sess-fresh-resume",
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "fresh", "mode": "agent"},
    )
    resume_input = InteractiveInput()
    resume_input.update(
        pending.key.invocation_id,
        {"approved": True, "auto_confirm": False, "feedback": ""},
    )
    resume_request = AgentRequest(
        request_id="request-resume",
        channel_id="web",
        session_id="sess-fresh-resume",
        req_method=ReqMethod.CHAT_SEND,
        params={
            "query": "resume",
            "mode": "agent",
            "source": "permission_interrupt",
        },
    )

    fresh_task = asyncio.create_task(
        adapter._prepare_root_input_dispatch(fresh_request, {"query": "fresh"})
    )
    await cutover_entered.wait()
    resume_started = asyncio.Event()

    async def prepare_resume():
        resume_started.set()
        return await adapter._prepare_root_input_dispatch(
            resume_request,
            {"query": resume_input},
        )

    resume_task = asyncio.create_task(prepare_resume())
    await resume_started.wait()

    assert adapter._permission_dispatch.lock.locked() is True
    assert resume_task.done() is False
    allow_cutover.set()
    fresh = await fresh_task
    loop_session.get_state.return_value = None
    adapter._permission_dispatch.release(fresh)
    with pytest.raises(
        RootPermissionQueueError,
        match="interaction_resume_state_missing",
    ):
        await resume_task


def test_callback_claim_retires_accepted_handoff_after_request_finalizes() -> None:
    queue = RootPermissionQueue(id_factory=lambda: "tiv-claimed-handoff")
    card, _state = _pending_permission_state(
        queue,
        session_id="sess-claimed-handoff",
    )
    pending = queue.get(card.key)
    assert pending is not None
    queue.reconcile(
        {
            "result_type": "interrupt",
            "interrupt_ids": [pending.key.tool_call_id],
            "state": [{"id": pending.key.tool_call_id, "value": pending.request}],
        },
        root_session_id="sess-claimed-handoff",
    )
    incoming = InteractiveInput()
    incoming.update(
        pending.key.invocation_id,
        {"approved": True, "auto_confirm": False, "feedback": ""},
    )
    answer = queue.reserve_answer("sess-claimed-handoff", incoming)
    handoff = RootPermissionDispatchHandoff(
        lock=asyncio.Lock(),
        root_session_id="sess-claimed-handoff",
        answer=answer,
        accepted=True,
    )
    adapter = _make_smart_adapter(
        _root_permission_queue=queue,
        dispatch_lock=handoff.lock,
        handoff=handoff,
    )
    inputs = {
        "_jiuwenswarm_root_permission_answer": answer,
        "_jiuwenswarm_root_permission_handoff": handoff,
    }

    adapter._permission_dispatch.finalize(inputs)
    assert adapter._has_live_root_permission_owner("sess-claimed-handoff") is True

    claim = queue.claim_answer_for_call(
        root_session_id="sess-claimed-handoff",
        execution_session_id="sess-claimed-handoff",
        tool_call_id=pending.key.tool_call_id,
        tool_name="bash",
    )
    adapter._permission_dispatch.close_claimed(claim.card.key)
    queue.finish(claim.card.key)

    assert handoff.closed is True
    assert adapter._permission_dispatch.handoff is None
    assert adapter._has_live_root_permission_owner("sess-claimed-handoff") is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [RuntimeError("send rejected"), asyncio.CancelledError()],
)
async def test_permission_resume_preaccept_failure_restores_exact_pending_card(
    failure: BaseException,
) -> None:
    queue = RootPermissionQueue(id_factory=lambda: "tiv-preaccept")
    card, _state = _pending_permission_state(queue, session_id="sess-preaccept")
    pending = queue.get(card.key)
    assert pending is not None
    queue.reconcile(
        {
            "result_type": "interrupt",
            "interrupt_ids": [pending.key.tool_call_id],
            "state": [{"id": pending.key.tool_call_id, "value": pending.request}],
        },
        root_session_id="sess-preaccept",
    )
    incoming = InteractiveInput()
    incoming.update(
        pending.key.invocation_id,
        {"approved": True, "auto_confirm": False, "feedback": ""},
    )
    answer = queue.reserve_answer("sess-preaccept", incoming)
    dispatch_lock = asyncio.Lock()
    await dispatch_lock.acquire()
    handoff = RootPermissionDispatchHandoff(
        lock=dispatch_lock,
        root_session_id="sess-preaccept",
        answer=answer,
    )
    instance = MagicMock()
    instance.send_input = AsyncMock(side_effect=failure)
    adapter = _make_smart_adapter(
        _instance=instance,
        _root_permission_queue=queue,
        dispatch_lock=dispatch_lock,
        handoff=None,
    )
    request = SendInputRequest(
        request_id="resume-preaccept",
        inputs={
            "query": answer.interactive_input,
            "_jiuwenswarm_root_permission_answer": answer,
            "_jiuwenswarm_root_permission_handoff": handoff,
        },
    )

    with pytest.raises(type(failure)):
        await adapter._send_input_with_permission_resume_guard(request)

    restored = queue.get(answer.card.key)
    assert restored is not None and restored.state == "pending"
    assert dispatch_lock.locked() is False
    assert handoff.closed is True
    assert adapter._permission_dispatch.handoff is None
    retry = queue.reserve_answer("sess-preaccept", incoming)
    assert retry.card.state == "resuming"
    queue.release_answer(retry)


@pytest.mark.asyncio
async def test_fresh_input_preserves_completed_sibling_context() -> None:
    queue = RootPermissionQueue(id_factory=lambda: "tiv-sibling")
    card, state = _pending_permission_state(queue, session_id="sess-sibling")
    completed_call = SimpleNamespace(id="call-complete", name="read_file")
    state.ai_message.tool_calls.insert(0, completed_call)
    loop_session, context, context_engine = _permission_core_state(
        "sess-sibling", state
    )
    completed_result = SimpleNamespace(
        role="tool",
        tool_call_id="call-complete",
        content="already completed",
    )
    context.get_messages.return_value = [state.ai_message, completed_result]
    instance = MagicMock()
    instance._loop_session = loop_session
    instance.react_agent = SimpleNamespace(context_engine=context_engine)
    instance.cancel_round = AsyncMock(return_value=False)
    adapter = _make_smart_adapter(_instance=instance, _root_permission_queue=queue)
    request = AgentRequest(
        request_id="request-new",
        channel_id="web",
        session_id="sess-sibling",
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "开始新任务", "mode": "agent"},
    )

    await adapter._prepare_root_input_dispatch(request, {"query": "开始新任务"})

    assert context.get_messages.return_value[-1] is completed_result
    assert context.add_messages.await_args.args[0].tool_call_id == card.key.tool_call_id
    context.pop_messages.assert_not_called()


@pytest.mark.asyncio
async def test_fresh_input_rejects_missing_completed_sibling_result() -> None:
    queue = RootPermissionQueue(id_factory=lambda: "tiv-missing-result")
    _card, state = _pending_permission_state(queue, session_id="sess-missing-result")
    state.ai_message.tool_calls.insert(
        0,
        SimpleNamespace(id="call-complete", name="read_file"),
    )
    loop_session, context, context_engine = _permission_core_state(
        "sess-missing-result", state
    )
    instance = MagicMock()
    instance._loop_session = loop_session
    instance.react_agent = SimpleNamespace(context_engine=context_engine)
    instance.cancel_round = AsyncMock(return_value=True)
    adapter = _make_smart_adapter(_instance=instance, _root_permission_queue=queue)
    request = AgentRequest(
        request_id="request-new",
        channel_id="web",
        session_id="sess-missing-result",
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "开始新任务", "mode": "agent"},
    )

    with pytest.raises(
        RootPermissionQueueError,
        match="permission_continuation_discard_failed",
    ):
        await adapter._prepare_root_input_dispatch(request, {"query": "开始新任务"})

    context.add_messages.assert_not_awaited()
    with pytest.raises(RootPermissionQueueError, match="permission_queue_quarantined"):
        queue.raise_if_quarantined("sess-missing-result")


@pytest.mark.asyncio
async def test_fresh_input_rolls_back_partial_synthetic_tool_results() -> None:
    invocation_ids = iter(("tiv-pending-one", "tiv-pending-two"))
    queue = RootPermissionQueue(id_factory=lambda: next(invocation_ids))
    entries = {}
    cards = []
    tool_calls = []
    for tool_call_id in ("call-one", "call-two"):
        card = _begin_invocation(
            queue,
            session_id="sess-partial-write",
            request_id="request-old",
            tool_call_id=tool_call_id,
        )
        request = InterruptRequest(
            message="approve",
            metadata={"tool_invocation_key": card.key.to_wire()},
        )
        queue.mark_pending(
            card.key,
            request=request,
            auto_manual=True,
            root_context=None,
        )
        tool_call = SimpleNamespace(id=tool_call_id, name="bash")
        entries[tool_call_id] = SimpleNamespace(
            tool_call=tool_call,
            interrupt_requests={card.key.tool_call_id: request},
        )
        cards.append(card)
        tool_calls.append(tool_call)
    state = SimpleNamespace(
        ai_message=SimpleNamespace(tool_calls=tool_calls),
        interrupted_tools=entries,
    )
    loop_session, context, context_engine = _permission_core_state(
        "sess-partial-write", state
    )
    context.add_messages.side_effect = [None, RuntimeError("write failed")]
    instance = MagicMock()
    instance._loop_session = loop_session
    instance.react_agent = SimpleNamespace(context_engine=context_engine)
    instance.cancel_round = AsyncMock(return_value=True)
    adapter = _make_smart_adapter(_instance=instance, _root_permission_queue=queue)
    request = AgentRequest(
        request_id="request-new",
        channel_id="web",
        session_id="sess-partial-write",
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "开始新任务", "mode": "agent"},
    )

    with pytest.raises(
        RootPermissionQueueError,
        match="permission_continuation_discard_failed",
    ):
        await adapter._prepare_root_input_dispatch(request, {"query": "开始新任务"})

    context.pop_messages.assert_called_once_with(1, with_history=True)
    assert all(queue.get(card.key) is not None for card in cards)
    with pytest.raises(RootPermissionQueueError, match="permission_queue_quarantined"):
        queue.raise_if_quarantined("sess-partial-write")


@pytest.mark.asyncio
async def test_fresh_input_rejects_unprovable_permission_discard() -> None:
    queue = RootPermissionQueue(id_factory=lambda: "tiv-stale")
    card, state = _pending_permission_state(queue, session_id="sess-stale")
    loop_session, context, context_engine = _permission_core_state("sess-stale", state)
    context.get_messages.return_value = [
        SimpleNamespace(tool_calls=[SimpleNamespace(id="different-call", name="bash")])
    ]
    instance = MagicMock()
    instance._loop_session = loop_session
    instance.react_agent = SimpleNamespace(context_engine=context_engine)
    instance.cancel_round = AsyncMock(return_value=True)
    adapter = _make_smart_adapter(_instance=instance, _root_permission_queue=queue)
    request = AgentRequest(
        request_id="request-new",
        channel_id="web",
        session_id="sess-stale",
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "继续执行", "mode": "agent"},
    )

    with pytest.raises(
        RootPermissionQueueError,
        match="permission_continuation_discard_failed",
    ):
        await adapter._prepare_root_input_dispatch(request, {"query": "继续执行"})

    assert queue.get(card.key) is not None
    with pytest.raises(RootPermissionQueueError, match="permission_queue_quarantined"):
        queue.raise_if_quarantined("sess-stale")
    context.add_messages.assert_not_awaited()


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


def test_permission_resume_dispatch_reuses_frozen_root_context() -> None:
    queue = RootPermissionQueue(id_factory=lambda: "invocation-1")
    context = RootDecisionContext(
        session_id="root-session",
        request_id="original-request",
        channel_id="web",
        trusted_turns=(
            RootIntentTurn(
                request_id="original-request",
                kind=RootIntentTurnKind.FRESH,
                text="Search the current project and summarize it",
            ),
        ),
    )
    card = queue.begin(
        root_session_id="root-session",
        request_id="root-request",
        execution_session_id="root-session",
        tool_call_id="call-1",
        tool_name="bash",
    )
    pending = queue.mark_pending(
        card.key,
        request=InterruptRequest(
            message="approve",
            metadata={"tool_invocation_key": card.key.to_wire()},
        ),
        auto_manual=True,
        root_context=context,
    )
    queue.reconcile(
        {
            "result_type": "interrupt",
            "interrupt_ids": ["call-1"],
            "state": [{"id": card.key.tool_call_id, "value": pending.request}],
        },
        root_session_id="root-session",
    )
    incoming = InteractiveInput()
    incoming.update(
        card.key.invocation_id,
        {"approved": True, "auto_confirm": False, "feedback": ""},
    )
    answer = queue.reserve_answer("root-session", incoming)
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._enable_auto_permission = True
    request = SimpleNamespace(
        params={
            "source": "permission_interrupt",
            "request_id": "resume-request",
            "answers": [{}],
            "mode": "agent",
        },
        request_id="resume-request",
        session_id="root-session",
        channel_id="web",
        req_method="chat.send",
    )

    prepared = adapter._with_root_context(
        request,
        {
            "query": answer.interactive_input,
            "_jiuwenswarm_root_permission_answer": answer,
        },
    )

    assert prepared["run"]["context"]["extra"][ROOT_CONTEXT_KEY] == context.to_mapping()
