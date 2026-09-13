# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for JiuWenSwarmDeepAdapter interrupt when stream consumer already unwound."""

from __future__ import annotations

import asyncio
import contextvars
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from openjiuwen.core.single_agent.interrupt.state import INTERRUPTION_KEY
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.agent_adapter import interface_deep
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
from jiuwenswarm.server.runtime.skill_turbo.permission_bridge import (
    SKILL_TURBO_RESUME_CTX_KEY,
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
    # process_interrupt reads these; __init__ normally sets them.
    adapter._task_planning_rail = None  # pylint: disable=protected-access
    adapter._workspace_dir = ""  # pylint: disable=protected-access
    for name, value in state.items():
        setattr(adapter, name, value)
    return adapter


def test_a2a_tool_route_survives_background_task_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(interface_deep, "get_runtime_tool_session_id", lambda: None)
    monkeypatch.setattr(interface_deep, "get_runtime_tool_channel_id", lambda: "default")
    adapter = _make_adapter(
        _current_request_route={
            "session_id": "web-session",
            "request_id": "request-1",
            "channel_id": "web",
        },
        _runtime_cron_tool_context=SimpleNamespace(
            session_id=None,
            channel_id="default",
        ),
    )

    assert adapter._get_a2a_outbound_tool_route() == ("web-session", "web")


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name, params", [
    ("a2a_find_agents", {"query": ""}),
    ("a2a_dispatch_task", {"agent_id": "weather", "task": "weather", "mode": "sync"}),
    ("a2a_get_dispatch", {"dispatch_id": "dispatch-1"}),
])
async def test_a2a_resource_survives_preexisting_runner_task(tool_name, params):
    """A runner created before request binding must use its adapter's identity."""
    ready = asyncio.Event()
    adapters = [_make_adapter(_current_request_route={}) for _ in range(2)]

    async def invoke(adapter):
        await ready.wait()
        tools = {}

        async def call(method, payload, **route):
            return {"params": payload, **route}

        rail = adapter._build_a2a_outbound_toolkit_rail()
        rail._backend_provider = lambda: SimpleNamespace(ready=True, call=call)
        rail.init(SimpleNamespace(
            ability_manager=SimpleNamespace(add_ability=lambda card, tool: tools.update({card.name: tool})),
            system_prompt_builder=None,
        ))
        return await tools[tool_name].invoke(params)

    runners = [
        asyncio.create_task(invoke(adapter), context=contextvars.Context())
        for adapter in adapters
    ]
    for index, adapter in enumerate(adapters):
        adapter._current_request_route = {
            "session_id": f"session-{index}", "channel_id": "web",
            "resource_id": f"bot-{index}",
        }
    ready.set()
    results = await asyncio.gather(*runners)
    for index, result in enumerate(results):
        assert result["session_id"] == f"session-{index}"
        assert result["params"]["resource_id"] == f"bot-{index}"


def test_a2a_bound_identity_does_not_borrow_snapshot_resource():
    adapter = _make_adapter(_current_request_route={
        "session_id": "old-session", "resource_id": "old-bot",
    })

    def check():
        interface_deep._CRON_TOOL_SESSION_ID.set("new-session")
        interface_deep._RUNTIME_TOOL_RESOURCE_ID.set("")
        assert adapter._get_a2a_outbound_tool_resource_id() == ""
        interface_deep._RUNTIME_TOOL_RESOURCE_ID.set("new-bot")
        assert adapter._get_a2a_outbound_tool_resource_id() == "new-bot"

    contextvars.Context().run(check)


def test_a2a_snapshot_missing_resource_does_not_borrow_cron_identity():
    adapter = _make_adapter(
        _current_request_route={"session_id": "new-session"},
        _runtime_cron_tool_context=SimpleNamespace(metadata={"routing": {"bot_id": "old-bot"}}),
    )
    assert contextvars.Context().run(adapter._get_a2a_outbound_tool_resource_id) == ""


@pytest.mark.parametrize("mode", ["team", "team.plan", "code.team"])
def test_deep_team_modes_leave_a2a_rail_to_swarm_provider(mode: str) -> None:
    adapter = _make_adapter(
        _model=None,
        _config_cache={},
        _filesystem_rail=None,
        _skill_protocol_prompt_rail=None,
        _task_execution_rail=None,
    )
    adapter._filesystem_rail_enabled_for_profile = lambda: False
    adapter._skill_include_tools_for_profile = lambda: True
    adapter._task_execution_rail_enabled = lambda: False
    adapter._instantiate_rails = lambda rail_infos, _config: rail_infos

    rail_infos = adapter._build_agent_rails({}, {}, mode=mode)

    assert "_a2a_outbound_toolkit_rail" not in {
        info.attr_name for info in rail_infos
    }


def _make_message_adapter(monkeypatch: pytest.MonkeyPatch) -> JiuWenSwarmDeepAdapter:
    adapter = JiuWenSwarmDeepAdapter()
    adapter._instance = SimpleNamespace(get_context_usage=lambda **_kwargs: {})
    adapter._is_session_scoped_adapter = True
    monkeypatch.setattr(adapter, "_has_valid_model_config", lambda _model_name="": True)
    monkeypatch.setattr(adapter, "_handle_slash_command", AsyncMock(return_value=None))
    monkeypatch.setattr(adapter, "_resolve_model_for_request", lambda _request: None)
    monkeypatch.setattr(adapter, "_apply_model_to_react_agent", lambda _model: None)
    return adapter


@pytest.mark.asyncio
@pytest.mark.parametrize("params_only", [False, True])
async def test_team_stream_binds_a2a_resource_context(
    monkeypatch: pytest.MonkeyPatch, params_only: bool,
) -> None:
    from jiuwenswarm.server.runtime.agent_adapter import team_helpers
    from jiuwenswarm.server.runtime import tenant_agent_pool

    adapter = _make_message_adapter(monkeypatch)
    adapter._deepresearch_rewrite_tx_uncertain = False
    adapter._config_cache = {}
    adapter._config_base_cache = {}
    adapter._runtime_prompt_rail = None
    adapter._project_dir = None
    adapter._workspace_dir = None
    monkeypatch.setattr(adapter, "_inject_extension_config_into_inputs", lambda _inputs: None)
    monkeypatch.setattr(adapter, "_try_skill_turbo_resume", AsyncMock(return_value=None))
    monkeypatch.setattr(adapter, "_arm_skill_turbo_interrupt_recovery_hint", AsyncMock())
    monkeypatch.setattr(adapter, "_deepresearch_artifact_output_dir", lambda _path: None)
    monkeypatch.setattr(
        adapter, "_prepare_multimodal_image_inputs", lambda _request, inputs: inputs
    )
    monkeypatch.setattr(adapter, "_native_image_input_enabled", lambda *_args: False)
    monkeypatch.setattr(
        adapter,
        "_prepare_react_image_tool_prompt",
        lambda _request, inputs, **_kwargs: inputs,
    )
    monkeypatch.setattr(adapter, "_resolve_runtime_language", lambda: "zh")
    monkeypatch.setattr(adapter, "_resolve_prompt_channel", lambda _session: "web")
    monkeypatch.setattr(adapter, "_write_runtime_state", lambda **_kwargs: None)
    monkeypatch.setattr(interface_deep, "set_perf_summary_context", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(interface_deep, "finalize_perf_summary_request", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(interface_deep, "clear_perf_summary_context", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(interface_deep, "resolve_tenant_sessions_dir", lambda _key: Path("."))
    monkeypatch.setattr(interface_deep, "evolution_slash_command_name", lambda _query: None)
    monkeypatch.setattr(
        tenant_agent_pool.TenantAgentPool,
        "extract_ids",
        staticmethod(lambda _request: ("agent", "service", "workspace")),
    )

    async def _team_stream(*_args, **_kwargs):
        assert interface_deep.get_runtime_tool_resource_id() == "resource-1"
        assert contextvars.Context().run(adapter._get_a2a_outbound_tool_resource_id) == "resource-1"
        yield SimpleNamespace(payload={"event_type": "chat.delta"})

    monkeypatch.setattr(team_helpers, "process_team_message_stream", _team_stream)
    request = AgentRequest(
        request_id="request-team",
        channel_id="web",
        session_id="session-team",
        is_stream=True,
        params={"query": "delegate", "mode": "team", "bot_id": "resource-1" if params_only else "untrusted"},
        metadata={} if params_only else {"routing": {"bot_id": "resource-1"}},
    )

    chunks = [
        chunk
        async for chunk in adapter.process_message_stream_impl(
            request, {"query": "delegate"}
        )
    ]

    assert len(chunks) == 1
    assert interface_deep.get_runtime_tool_resource_id() == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("params_only", [False, True])
async def test_nonstream_entry_captures_a2a_resource_snapshot(monkeypatch, params_only):
    adapter = _make_message_adapter(monkeypatch)
    monkeypatch.setattr(adapter, "_deepresearch_artifact_output_dir", lambda _path: "")
    monkeypatch.setattr(adapter, "_handle_slash_command", AsyncMock(return_value={"result_type": "goal_control"}))
    request = AgentRequest(
        request_id="snapshot-request", channel_id="web", session_id="snapshot-session",
        params={"query": "test", "mode": "agent", "bot_id": "resource-1" if params_only else "untrusted"},
        metadata={} if params_only else {"routing": {"bot_id": "resource-1"}},
    )
    await adapter.process_message_impl(request, {"query": "test"})
    assert contextvars.Context().run(adapter._get_a2a_outbound_tool_resource_id) == "resource-1"


def _route_test_request(*, stream: bool = False) -> AgentRequest:
    return AgentRequest(
        request_id="req-pre-try",
        channel_id="tui",
        session_id="sess-pre-try",
        params={"query": "research", "mode": "agent"},
        is_stream=stream,
    )


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

    assert loop_session.update_state.call_args_list == [
        call({INTERRUPTION_KEY: None}),
        call({SKILL_TURBO_RESUME_CTX_KEY: None}),
    ]
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
        reset_shell_session_id,
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
    adapter = _make_adapter()
    try:
        getattr(adapter, "_reset_runtime_cron_context")(
            interface_deep._RuntimeCronContextTokens(
                channel=MagicMock(),
                session=MagicMock(),
                metadata=MagicMock(),
                mode=MagicMock(),
                bound=MagicMock(),
                shell=shell_token,
                deepresearch=None,
                send_file=None,
            )
        )
        reset_shell_mock.assert_called_once_with(shell_token)
    finally:
        reset_shell_session_id(shell_token)


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

    adapter = _make_adapter()
    tokens = adapter._bind_runtime_cron_context(
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
        adapter._reset_runtime_cron_context(tokens)


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
    adapter = _make_adapter()
    tokens = adapter._bind_runtime_cron_context(
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
        adapter._reset_runtime_cron_context(tokens)

    assert context.session_id == "sess_runtime"
    assert context.mode == "agent"
    metadata = context.metadata
    assert metadata["request_id"] == "req-runtime"
    assert metadata["project_id"] == "proj_runtime"
    assert metadata["project_dir"] == "D:\\runtime-project"
    assert metadata["work_mode"] == "work"


def test_stream_consumer_cancellation_resets_only_deepresearch_route() -> None:
    from jiuwenswarm.agents.harness.common.tools.deepresearch.tools import _get_route
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    adapter = _make_adapter(
        _env_service_id="service-cancel",
        _env_agent_id="agent-cancel",
    )
    tokens = adapter._bind_runtime_cron_context(
        channel_id="tui",
        session_id="sess-cancel",
        metadata={},
        request_id="req-cancel",
        mode="agent",
    )

    adapter._reset_stream_runtime_context(
        tokens,
        stream_consumer_cancelled=True,
    )
    try:
        assert _get_route()["session_id"] == ""
        assert interface_deep._CRON_TOOL_SESSION_ID.get() == "sess-cancel"
    finally:
        adapter._reset_runtime_cron_context(tokens)


def test_stream_normal_completion_resets_route_and_legacy_context_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.agents.harness.common.tools.deepresearch.tools import _get_route

    adapter = _make_adapter(
        _env_service_id="service-normal",
        _env_agent_id="agent-normal",
    )
    tokens = adapter._bind_runtime_cron_context(
        channel_id="web",
        session_id="sess-normal",
        metadata={},
        request_id="req-normal",
        mode="agent",
    )
    reset_runtime = MagicMock(wraps=adapter._reset_runtime_cron_context)
    monkeypatch.setattr(adapter, "_reset_runtime_cron_context", reset_runtime)

    adapter._reset_stream_runtime_context(
        tokens,
        stream_consumer_cancelled=False,
    )

    reset_runtime.assert_called_once_with(tokens)
    assert _get_route()["session_id"] == ""


def test_runtime_route_binds_current_session_workspace(tmp_path: Path) -> None:
    from jiuwenswarm.agents.harness.common.tools.deepresearch import tools as dt

    session_workspace = tmp_path / "current-session-workspace"
    adapter = _make_adapter(
        _env_service_id="service-session-output",
        _env_agent_id="agent-session-output",
        _workspace_dir=str(tmp_path / "agent-workspace"),
    )
    tokens = adapter._bind_runtime_cron_context(
        channel_id="officeclaw",
        session_id="sess-session-output",
        metadata={},
        request_id="req-session-output",
        mode="agent",
        project_dir=str(session_workspace),
    )
    try:
        # Generation and rewrite tools both consume this routed output root.
        assert dt._get_effective_request_output_dir() == session_workspace.resolve()
    finally:
        adapter._reset_runtime_cron_context(tokens)


def test_runtime_route_binds_and_resets_enterprise_a2a_identity() -> None:
    enterprise = SimpleNamespace(
        resource_id="resource-1",
        template_ref={"a2a_access_policy": ["policy-1"]},
    )
    adapter = _make_adapter(
        _enterprise_config=enterprise,
        _env_service_id="service-1",
        _env_agent_id="agent-1",
    )
    tokens = adapter._bind_runtime_cron_context(
        channel_id="web",
        session_id="session-1",
        metadata={"routing": {"bot_id": "resource-1"}},
        request_id="request-1",
        mode="agent",
    )
    try:
        assert interface_deep.get_runtime_tool_resource_id() == "resource-1"
        assert interface_deep.get_runtime_tool_a2a_policy_id() == "policy-1"
    finally:
        adapter._reset_runtime_cron_context(tokens)

    assert interface_deep.get_runtime_tool_resource_id() == ""
    assert interface_deep.get_runtime_tool_a2a_policy_id() == ""


def test_deepresearch_tool_context_keeps_session_workspace_snapshot(
    tmp_path: Path,
) -> None:
    session_workspace = tmp_path / "current-session-workspace"
    adapter = _make_adapter(
        _current_request_route={
            "session_id": "sess-background-output",
            "request_id": "req-background-output",
            "channel_id": "officeclaw",
            "output_dir": str(session_workspace),
        },
        _runtime_cron_tool_context=SimpleNamespace(
            session_id=None,
            channel_id="default",
            metadata={},
        ),
        _env_service_id="service-background-output",
        _env_agent_id="agent-background-output",
        _workspace_dir=str(tmp_path / "agent-workspace"),
    )

    context = adapter._get_deepresearch_tool_context()

    assert Path(context["output_dir"]) == session_workspace.resolve()


def test_artifact_output_uses_session_project_when_request_omits_workspace(
    tmp_path: Path,
) -> None:
    session_workspace = tmp_path / "session-project"
    adapter = _make_adapter(
        _project_dir=str(session_workspace),
        _workspace_dir=str(tmp_path / "agent-workspace"),
    )

    assert Path(adapter._deepresearch_artifact_output_dir()) == (
        session_workspace.resolve()
    )


def test_runtime_route_binds_adapter_artifact_output_dir(tmp_path: Path) -> None:
    from jiuwenswarm.agents.harness.common.tools.deepresearch import tools as dt

    agent_workspace = tmp_path / "agent-workspace"
    adapter = _make_adapter(
        _env_service_id="service-output",
        _env_agent_id="agent-output",
        _workspace_dir=str(agent_workspace),
    )
    tokens = adapter._bind_runtime_cron_context(
        channel_id="officeclaw",
        session_id="sess-output",
        metadata={},
        request_id="req-output",
        mode="agent",
    )
    try:
        assert dt._get_effective_request_output_dir() == (
            agent_workspace / "projects"
        ).resolve()
    finally:
        adapter._reset_runtime_cron_context(tokens)


def test_runtime_route_binds_request_workspace_as_artifact_output_dir(
    tmp_path: Path,
) -> None:
    from jiuwenswarm.agents.harness.common.tools.deepresearch import tools as dt

    agent_workspace = tmp_path / "agent-workspace"
    request_workspace = tmp_path / "session-workspace"
    adapter = _make_adapter(
        _env_service_id="service-output",
        _env_agent_id="agent-output",
        _workspace_dir=str(agent_workspace),
    )
    tokens = adapter._bind_runtime_cron_context(
        channel_id="officeclaw",
        session_id="sess-output",
        metadata={},
        request_id="req-output",
        mode="agent",
        project_dir=str(request_workspace),
    )
    try:
        assert dt._get_effective_request_output_dir() == request_workspace.resolve()
    finally:
        adapter._reset_runtime_cron_context(tokens)


def test_progressive_deepresearch_context_keeps_request_workspace(
    tmp_path: Path,
) -> None:
    request_workspace = tmp_path / "session-workspace"
    adapter = _make_adapter(
        _env_service_id="service-output",
        _env_agent_id="agent-output",
    )
    adapter._current_request_route = {
        "session_id": "sess-output",
        "request_id": "req-output",
        "channel_id": "officeclaw",
        "output_dir": str(request_workspace),
    }

    assert adapter._get_deepresearch_tool_context()["output_dir"] == str(
        request_workspace.resolve()
    )


def test_runtime_route_keeps_artifact_leaf_lexical(tmp_path: Path) -> None:
    agent_workspace = tmp_path / "agent-workspace"
    outside = tmp_path / "outside"
    agent_workspace.mkdir()
    outside.mkdir()
    (agent_workspace / "projects").symlink_to(
        outside, target_is_directory=True
    )
    adapter = _make_adapter(
        _env_service_id="service-output",
        _env_agent_id="agent-output",
        _workspace_dir=str(agent_workspace),
    )

    assert adapter._deepresearch_artifact_output_dir() == str(
        agent_workspace.resolve() / "projects"
    )


@pytest.mark.parametrize("failure_type", [RuntimeError, asyncio.CancelledError])
def test_route_bind_failure_rolls_back_legacy_context(
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[BaseException],
) -> None:
    from openjiuwen.core.sys_operation.shell_process_registry import get_shell_session_id
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    adapter = _make_adapter(
        _env_service_id="service-bind-failure",
        _env_agent_id="agent-bind-failure",
    )
    previous_shell_session_id = get_shell_session_id()

    def fail_route_bind(**_kwargs):
        raise failure_type("route bind failed")

    monkeypatch.setattr(interface_deep, "push_deepresearch_route", fail_route_bind)

    with pytest.raises(failure_type, match="route bind failed"):
        adapter._bind_runtime_cron_context(
            channel_id="tui",
            session_id="sess-bind-failure",
            metadata={},
            request_id="req-bind-failure",
            mode="agent",
        )

    assert interface_deep._CRON_TOOL_SESSION_ID.get() is None
    assert interface_deep._CRON_TOOL_BOUND.get() is False
    assert get_shell_session_id() == previous_shell_session_id


def test_full_reset_continues_after_failure_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    reset_shell = MagicMock()
    monkeypatch.setattr(
        "openjiuwen.core.sys_operation.shell_process_registry.reset_shell_session_id",
        reset_shell,
    )
    route_reset = MagicMock(side_effect=RuntimeError("route reset failed"))
    monkeypatch.setattr(interface_deep, "reset_deepresearch_route", route_reset)
    context_vars = {}
    for name in (
        "_CRON_TOOL_BOUND",
        "_CRON_TOOL_MODE",
        "_CRON_TOOL_METADATA",
        "_CRON_TOOL_SESSION_ID",
        "_CRON_TOOL_CHANNEL_ID",
    ):
        context_vars[name] = MagicMock()
        monkeypatch.setattr(interface_deep, name, context_vars[name])
    tokens = interface_deep._RuntimeCronContextTokens(
        channel=MagicMock(),
        session=MagicMock(),
        metadata=MagicMock(),
        mode=MagicMock(),
        bound=MagicMock(),
        shell=MagicMock(),
        deepresearch=interface_deep._DeepResearchRouteContextToken(MagicMock()),
    )

    with pytest.raises(RuntimeError, match="route reset failed"):
        JiuWenSwarmDeepAdapter._reset_runtime_cron_context(tokens)

    reset_shell.assert_called_once()
    for context_var in context_vars.values():
        context_var.reset.assert_called_once()

    with pytest.raises(RuntimeError, match="route reset failed"):
        JiuWenSwarmDeepAdapter._reset_runtime_cron_context(tokens)
    assert route_reset.call_count == 2
    reset_shell.assert_called_once()
    for context_var in context_vars.values():
        context_var.reset.assert_called_once()


def test_cross_context_reset_retains_tokens_for_retry_in_original_context() -> None:
    from jiuwenswarm.agents.harness.common.tools.deepresearch.tools import _get_route

    adapter = _make_adapter(
        _env_service_id="service-cross-context",
        _env_agent_id="agent-cross-context",
    )
    tokens = adapter._bind_runtime_cron_context(
        channel_id="tui",
        session_id="sess-cross-context",
        metadata={},
        request_id="req-cross-context",
        mode="agent",
    )

    with pytest.raises(ValueError):
        contextvars.Context().run(adapter._reset_runtime_cron_context, tokens)

    assert tokens.channel is not None
    assert tokens.session is not None
    assert tokens.metadata is not None
    assert tokens.mode is not None
    assert tokens.bound is not None
    assert tokens.shell is not None
    assert tokens.deepresearch is not None and tokens.deepresearch.token is not None

    adapter._reset_runtime_cron_context(tokens)
    adapter._reset_runtime_cron_context(tokens)
    assert _get_route()["session_id"] == ""
    assert all(
        token is None
        for token in (
            tokens.channel,
            tokens.session,
            tokens.metadata,
            tokens.mode,
            tokens.bound,
            tokens.shell,
        )
    )
    assert tokens.deepresearch is not None and tokens.deepresearch.token is None


def test_bind_rollback_preserves_cancelled_error_when_shell_reset_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    adapter = _make_adapter(
        _env_service_id="service-bind-failure",
        _env_agent_id="agent-bind-failure",
    )

    def fail_route_bind(**_kwargs):
        raise asyncio.CancelledError("original cancellation")

    monkeypatch.setattr(interface_deep, "push_deepresearch_route", fail_route_bind)
    monkeypatch.setattr(
        "openjiuwen.core.sys_operation.shell_process_registry.reset_shell_session_id",
        MagicMock(side_effect=RuntimeError("shell cleanup failed")),
    )

    with pytest.raises(asyncio.CancelledError, match="original cancellation"):
        adapter._bind_runtime_cron_context(
            channel_id="tui",
            session_id="sess-bind-failure",
            metadata={},
            request_id="req-bind-failure",
            mode="agent",
        )

    assert interface_deep._CRON_TOOL_CHANNEL_ID.get() == "web"
    assert interface_deep._CRON_TOOL_SESSION_ID.get() is None
    assert interface_deep._CRON_TOOL_METADATA.get() is None
    assert interface_deep._CRON_TOOL_MODE.get() is None
    assert interface_deep._CRON_TOOL_BOUND.get() is False


@pytest.mark.asyncio
async def test_chat_pre_try_failure_cleans_route_and_initialized_permission_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.agents.harness.common.tools.deepresearch.tools import _get_route
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    adapter = _make_message_adapter(monkeypatch)
    permission_token = object()
    permission_session_token = object()
    cleanup_permission = MagicMock()
    reset_permission_session = MagicMock()
    monkeypatch.setattr(
        interface_deep,
        "setup_permission_context",
        MagicMock(return_value=permission_token),
    )
    monkeypatch.setattr(interface_deep, "cleanup_permission_context", cleanup_permission)
    monkeypatch.setattr(
        interface_deep,
        "setup_permissions_session_scope",
        MagicMock(return_value=permission_session_token),
    )
    monkeypatch.setattr(
        interface_deep,
        "reset_permissions_session_scope",
        reset_permission_session,
    )
    monkeypatch.setattr(
        adapter,
        "_resolve_model_for_request",
        MagicMock(side_effect=RuntimeError("model init failed")),
    )

    with pytest.raises(RuntimeError, match="model init failed"):
        await adapter.process_message_impl(
            _route_test_request(),
            {"query": "research"},
        )

    assert _get_route()["session_id"] == ""
    assert interface_deep._CRON_TOOL_SESSION_ID.get() is None
    cleanup_permission.assert_called_once_with(permission_token)
    reset_permission_session.assert_called_once_with(permission_session_token)


@pytest.mark.asyncio
async def test_stream_pre_try_cancellation_cleans_route_and_initialized_permission_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.agents.harness.common.tools.deepresearch.tools import _get_route
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    adapter = _make_message_adapter(monkeypatch)
    permission_token = object()
    permission_session_token = object()
    cleanup_permission = MagicMock()
    reset_permission_session = MagicMock()
    monkeypatch.setattr(
        interface_deep,
        "setup_permission_context",
        MagicMock(return_value=permission_token),
    )
    monkeypatch.setattr(interface_deep, "cleanup_permission_context", cleanup_permission)
    monkeypatch.setattr(
        interface_deep,
        "setup_permissions_session_scope",
        MagicMock(return_value=permission_session_token),
    )
    monkeypatch.setattr(
        interface_deep,
        "reset_permissions_session_scope",
        reset_permission_session,
    )
    monkeypatch.setattr(
        adapter,
        "_maybe_apply_pending_reload",
        AsyncMock(side_effect=asyncio.CancelledError("pre-try cancelled")),
    )

    with pytest.raises(asyncio.CancelledError, match="pre-try cancelled"):
        async for _ in adapter.process_message_stream_impl(
            _route_test_request(stream=True),
            {"query": "research"},
        ):
            pass

    assert _get_route()["session_id"] == ""
    assert interface_deep._CRON_TOOL_SESSION_ID.get() is None
    cleanup_permission.assert_called_once_with(permission_token)
    reset_permission_session.assert_called_once_with(permission_session_token)


@pytest.mark.asyncio
async def test_chat_failure_survives_permission_cleanup_error_and_reaches_route_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    adapter = _make_message_adapter(monkeypatch)
    route_reset = MagicMock(wraps=interface_deep.reset_deepresearch_route)
    monkeypatch.setattr(interface_deep, "reset_deepresearch_route", route_reset)
    monkeypatch.setattr(
        interface_deep,
        "setup_permission_context",
        MagicMock(return_value=object()),
    )
    monkeypatch.setattr(
        interface_deep,
        "cleanup_permission_context",
        MagicMock(side_effect=RuntimeError("permission cleanup failed")),
    )
    monkeypatch.setattr(
        adapter,
        "_update_runtime_config",
        AsyncMock(side_effect=RuntimeError("chat execution failed")),
    )

    with pytest.raises(RuntimeError, match="chat execution failed"):
        await adapter.process_message_impl(
            _route_test_request(),
            {"query": "research"},
        )

    route_reset.assert_called_once()


@pytest.mark.asyncio
async def test_stream_consumer_cancellation_survives_permission_cleanup_error_and_resets_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    adapter = _make_message_adapter(monkeypatch)
    route_reset = MagicMock(wraps=interface_deep.reset_deepresearch_route)
    full_reset = MagicMock(wraps=adapter._reset_runtime_cron_context)
    monkeypatch.setattr(interface_deep, "reset_deepresearch_route", route_reset)
    monkeypatch.setattr(adapter, "_reset_runtime_cron_context", full_reset)
    monkeypatch.setattr(
        interface_deep,
        "setup_permission_context",
        MagicMock(return_value=object()),
    )
    monkeypatch.setattr(
        interface_deep,
        "cleanup_permission_context",
        MagicMock(side_effect=RuntimeError("permission cleanup failed")),
    )
    monkeypatch.setattr(
        adapter,
        "_update_runtime_config",
        AsyncMock(side_effect=asyncio.CancelledError("consumer cancelled")),
    )

    async def consume() -> None:
        async for _ in adapter.process_message_stream_impl(
            _route_test_request(stream=True),
            {"query": "research"},
        ):
            pass

    task = asyncio.create_task(consume())
    with pytest.raises(asyncio.CancelledError, match="consumer cancelled"):
        await task

    route_reset.assert_called_once()
    full_reset.assert_not_called()
