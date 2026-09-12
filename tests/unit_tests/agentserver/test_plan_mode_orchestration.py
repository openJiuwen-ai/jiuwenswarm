# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""End-to-end orchestration tests for plan mode — aligned with Claude Code.

Plan approval is now handled by ``PlanApprovalInterruptRail`` which intercepts
``exit_plan_mode`` with an immediate approval dialog.  Mode restoration happens
inside ``ExitPlanModeTool.invoke()`` via ``restore_mode_after_plan_exit()``.
The server-side pending-approval gate has been removed.
"""

# pylint: disable=protected-access

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _default_ctx(server, request):
    import asyncio as _asyncio

    from jiuwenswarm.server.context import AgentServerServices, RequestContext
    from jiuwenswarm.server.transports.sink import WSSink

    class _NullWs:
        async def send(self, text):  # noqa: ANN001
            return None

    _ws = _NullWs()
    return RequestContext(
        request=request,
        sink=WSSink(_ws, _asyncio.Lock()),
        connection_id=str(id(_ws)),
        services=AgentServerServices(server),
    )



from jiuwenswarm.server.handlers import _default

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server import agent_ws_server as agent_ws_server_module
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    _INTERRUPT_OUTPUT_ATTACH_RETRY_COUNT,
    _INTERRUPT_OUTPUT_ATTACH_RETRY_INTERVAL_SECONDS,
    _INTERRUPT_OUTPUT_ATTACH_SLOW_RETRY_COUNT,
    _INTERRUPT_OUTPUT_ATTACH_SLOW_RETRY_INTERVAL_SECONDS,
    JiuWenSwarmDeepAdapter,
)


def _chat_request(
    session_id: str,
    query: str = "hello",
    *,
    mode: str = "code.plan",
    extra_params: dict | None = None,
) -> AgentRequest:
    params: dict = {"query": query, "mode": mode}
    if extra_params:
        params.update(extra_params)
    return AgentRequest(
        request_id="req_flow",
        channel_id="tui",
        session_id=session_id,
        req_method=ReqMethod.CHAT_SEND,
        params=params,
    )


def test_approved_plan_exit_resume_is_detected() -> None:
    params = {
        "source": "confirm_interrupt",
        "request_id": "exit_plan_call_1",
        "answers": [{"selected_options": ["approve"]}],
    }

    assert (
        JiuWenSwarmDeepAdapter._approved_plan_exit_resume_tool_call_id(params)
        == "exit_plan_call_1"
    )
    assert (
        JiuWenSwarmDeepAdapter._approved_plan_exit_resume_tool_call_id(
            {**params, "answers": [{"selected_options": ["reject"]}]}
        )
        == ""
    )


@pytest.mark.asyncio
async def test_prepare_code_mode_chat_turn_resolves_mode_and_agent() -> None:
    """_prepare_code_mode_chat_turn resolves mode/sub_mode and gets the agent
    without plan-approval side effects.
    """
    session_id = "sess_basic"

    agent = MagicMock()
    manager = MagicMock()
    manager.get_agent = AsyncMock(return_value=agent)
    manager.wait_for_session_prewarm = AsyncMock()

    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._agent_manager = manager
    server._resolve_code_language = MagicMock(return_value="cn")

    request = _chat_request(session_id, "hello", mode="code.plan")

    mode, sub_mode, resolved_agent = await _default._prepare_code_mode_chat_turn(
        _default_ctx(server, request),
        request, "tui"
    )

    assert mode == "code"
    assert sub_mode == "plan"
    assert resolved_agent is agent
    manager.get_agent.assert_awaited_once()
    manager.wait_for_session_prewarm.assert_awaited_once_with(session_id)


@pytest.mark.asyncio
async def test_prepare_chat_normalizes_agent_request_for_code_workspace() -> None:
    agent = MagicMock()
    manager = MagicMock()
    manager.get_agent = AsyncMock(return_value=agent)
    manager.wait_for_session_prewarm = AsyncMock()
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._agent_manager = manager
    request = _chat_request(
        "sess_code_workspace",
        mode="agent",
        extra_params={"work_mode": "code", "project_dir": "/tmp/code-project"},
    )

    with patch(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        return_value={},
    ), patch.object(
        _default,
        "_sync_chat_request_metadata",
        return_value="/tmp/code-project",
    ):
        mode, sub_mode, resolved_agent = await _default._prepare_code_mode_chat_turn(
            _default_ctx(server, request),
            request,
            "web",
        )

    assert (mode, sub_mode, resolved_agent) == ("code", "normal", agent)
    assert request.params["mode"] == "code.normal"
    manager.get_agent.assert_awaited_once_with(
        channel_id="web",
        mode="code",
        project_dir="/tmp/code-project",
        sub_mode="normal",
    )


@pytest.mark.asyncio
async def test_prepare_team_chat_turn_propagates_locked_project_dir() -> None:
    """The session-locked project dir reaches TeamSpec request metadata.

    Web ``chat.send`` requests do not repeat ``project_dir``. Team assembly reads
    it from ``request.metadata``, so the effective value restored from session
    metadata must be propagated after synchronization.
    """
    agent = MagicMock()
    manager = MagicMock()
    manager.get_agent = AsyncMock(return_value=agent)
    manager.wait_for_session_prewarm = AsyncMock()

    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._agent_manager = manager
    request = _chat_request("sess_team_project", mode="team")
    request.metadata = {"member_name": "reviewer", "project_dir": "/tmp/stale"}

    with patch.object(
        _default,
        "_sync_chat_request_metadata",
        return_value=" /tmp/locked-project ",
    ):
        mode, sub_mode, resolved_agent = await _default._prepare_code_mode_chat_turn(
            _default_ctx(server, request),
            request, "web"
        )

    assert mode == "team"
    assert sub_mode is None
    assert resolved_agent is agent
    assert request.params["project_dir"] == "/tmp/locked-project"
    assert request.metadata == {
        "member_name": "reviewer",
        "project_dir": "/tmp/locked-project",
    }
    manager.get_agent.assert_awaited_once_with(
        channel_id="web",
        mode="team",
        project_dir="/tmp/locked-project",
        sub_mode=None,
    )
    manager.wait_for_session_prewarm.assert_awaited_once_with("sess_team_project")


@pytest.mark.asyncio
async def test_ensure_code_mode_state_syncs_plan_to_normal() -> None:
    """_ensure_code_mode_state syncs plan→normal when modes differ."""
    session_id = "sess_sync"

    plan_agent = MagicMock()
    plan_instance = MagicMock()
    plan_agent.get_instance.return_value = plan_instance
    # Non-chat callers now await ensure_instance(), which builds the root
    # DeepAgent on demand instead of relying on eager construction.
    plan_agent.ensure_instance = AsyncMock(return_value=plan_instance)
    plan_instance.card = SimpleNamespace(id="code-agent")
    plan_state = SimpleNamespace(mode="plan", plan_slug="test")
    plan_instance.load_state.return_value = SimpleNamespace(plan_mode=plan_state)

    session = MagicMock()
    create_session = MagicMock(return_value=session)
    pre_run = AsyncMock()
    post_run = AsyncMock()

    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    request = _chat_request(session_id, "hello", mode="code.normal")

    with patch(
        "openjiuwen.core.single_agent.create_agent_session",
        create_session,
    ):
        session.pre_run = pre_run
        session.post_run = post_run
        restored = await _default._ensure_code_mode_state(
            _default_ctx(server, request),
            request, "code", "normal", plan_agent
        )

    assert restored is True
    plan_instance.switch_mode.assert_called_once_with(session=session, mode="normal")


@pytest.mark.asyncio
async def test_ensure_code_mode_state_skips_if_mode_already_matches() -> None:
    """_ensure_code_mode_state does nothing when plan_mode already matches."""
    session_id = "sess_skip"

    plan_agent = MagicMock()
    plan_instance = MagicMock()
    plan_agent.get_instance.return_value = plan_instance
    # Non-chat callers now await ensure_instance(), which builds the root
    # DeepAgent on demand instead of relying on eager construction.
    plan_agent.ensure_instance = AsyncMock(return_value=plan_instance)
    plan_instance.card = SimpleNamespace(id="code-agent")
    plan_state = SimpleNamespace(mode="plan", plan_slug="test")
    plan_instance.load_state.return_value = SimpleNamespace(plan_mode=plan_state)

    session = MagicMock()
    create_session = MagicMock(return_value=session)

    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    request = _chat_request(session_id, "hello", mode="code.plan")

    with patch(
        "openjiuwen.core.single_agent.create_agent_session",
        create_session,
    ):
        session.pre_run = AsyncMock()
        session.post_run = AsyncMock()
        restored = await _default._ensure_code_mode_state(
            _default_ctx(server, request),
            request, "code", "plan", plan_agent
        )

    assert restored is False
    plan_instance.switch_mode.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_code_mode_state_allows_explicit_plan_reentry_after_exit(monkeypatch) -> None:
    """A user-triggered /plan re-entry must not be blocked by stale exit guards."""
    session_id = "sess_explicit_reentry"

    plan_agent = MagicMock()
    plan_instance = MagicMock()
    plan_agent.get_instance.return_value = plan_instance
    # Non-chat callers now await ensure_instance(), which builds the root
    # DeepAgent on demand instead of relying on eager construction.
    plan_agent.ensure_instance = AsyncMock(return_value=plan_instance)
    plan_instance.card = SimpleNamespace(id="code-agent")
    plan_state = SimpleNamespace(mode="normal", plan_slug="old-plan")
    plan_instance.load_state.return_value = SimpleNamespace(plan_mode=plan_state)

    session = MagicMock()
    create_session = MagicMock(return_value=session)

    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    _push_mock = AsyncMock()
    monkeypatch.setattr(_default, "_push_plan_mode_exited", _push_mock)
    request = _chat_request(
        session_id,
        "implement this in plan mode",
        mode="code.plan",
        extra_params={"plan_entry_source": "slash_command"},
    )

    agent_ws_server_module._plan_exited_sessions.add(session_id)
    try:
        with patch(
            "openjiuwen.core.single_agent.create_agent_session",
            create_session,
        ):
            session.pre_run = AsyncMock()
            session.post_run = AsyncMock()
            restored = await _default._ensure_code_mode_state(
                _default_ctx(server, request),
                request, "code", "plan", plan_agent
            )
    finally:
        agent_ws_server_module._plan_exited_sessions.discard(session_id)

    assert restored is False
    plan_instance.switch_mode.assert_called_once_with(session=session, mode="plan")
    _push_mock.assert_not_awaited()
    assert request.params["mode"] == "code.plan"


@pytest.mark.asyncio
async def test_ensure_code_mode_state_allows_e2a_plan_reentry_after_exit(
    monkeypatch,
) -> None:
    """E2A/officeclaw clients mark explicit plan entry with plan_entry_source=e2a."""
    session_id = "sess_e2a_reentry"

    plan_agent = MagicMock()
    plan_instance = MagicMock()
    plan_agent.get_instance.return_value = plan_instance
    plan_agent.ensure_instance = AsyncMock(return_value=plan_instance)
    plan_instance.card = SimpleNamespace(id="code-agent")
    plan_state = SimpleNamespace(mode="normal", plan_slug="old-plan")
    plan_instance.load_state.return_value = SimpleNamespace(plan_mode=plan_state)

    session = MagicMock()
    create_session = MagicMock(return_value=session)

    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    _push_mock = AsyncMock()
    monkeypatch.setattr(_default, "_push_plan_mode_exited", _push_mock)
    request = _chat_request(
        session_id,
        "continue in plan mode",
        mode="code.plan",
        extra_params={"plan_entry_source": "e2a"},
    )

    agent_ws_server_module._plan_exited_sessions.add(session_id)
    try:
        with patch(
            "openjiuwen.core.single_agent.create_agent_session",
            create_session,
        ):
            session.pre_run = AsyncMock()
            session.post_run = AsyncMock()
            restored = await _default._ensure_code_mode_state(
                _default_ctx(server, request),
                request, "code", "plan", plan_agent
            )
    finally:
        agent_ws_server_module._plan_exited_sessions.discard(session_id)

    assert restored is False
    plan_instance.switch_mode.assert_called_once_with(session=session, mode="plan")
    _push_mock.assert_not_awaited()
    assert request.params["mode"] == "code.plan"


def test_explicit_plan_entry_accepts_only_known_sources() -> None:
    e2a_request = _chat_request("sess", extra_params={"plan_entry_source": "e2a"})
    slash_request = _chat_request(
        "sess", extra_params={"plan_entry_source": "slash_command"}
    )
    stale_request = _chat_request("sess")
    unknown_request = _chat_request(
        "sess", extra_params={"plan_entry_source": "always-present-client-marker"}
    )

    assert _default._is_explicit_plan_entry_request(e2a_request) is True
    assert _default._is_explicit_plan_entry_request(slash_request) is True
    assert _default._is_explicit_plan_entry_request(stale_request) is False
    assert _default._is_explicit_plan_entry_request(unknown_request) is False


@pytest.mark.asyncio
async def test_tenant_unary_plan_exit_cleanup_runs_when_processing_fails() -> None:
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    agent = MagicMock()
    pool = MagicMock()
    pool.process_message = AsyncMock(side_effect=RuntimeError("send failed"))
    server._tenant_pool = MagicMock(return_value=pool)
    request = _chat_request("sess_unary_failure")
    ctx = _default_ctx(server, request)
    check_exit = AsyncMock()

    with patch.object(_default, "_uses_tenant_pool", return_value=True), patch.object(
        _default,
        "_prepare_tenant_code_mode_chat_turn",
        new=AsyncMock(return_value=("code", "plan", agent)),
    ), patch.object(
        _default, "_check_post_process_plan_exit", new=check_exit
    ), patch(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep.ensure_persistent_checkpointer",
        new=AsyncMock(),
    ):
        with pytest.raises(RuntimeError, match="send failed"):
            await _default._handle_unary_impl(ctx, request)

    check_exit.assert_awaited_once_with(ctx, request, agent)


@pytest.mark.asyncio
async def test_interrupt_output_reattach_retries_until_lease_is_released(
    monkeypatch,
) -> None:
    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    stream = object()
    adapter._instance = SimpleNamespace(
        attach_output=AsyncMock(side_effect=[None, None, stream])
    )
    sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep)

    result = await adapter._reattach_interrupt_output("sess-reattach")

    assert result is stream
    assert adapter._instance.attach_output.await_count == 3
    assert sleep.await_count == 3


@pytest.mark.asyncio
async def test_interrupt_output_reattach_extends_window_with_slow_tail(
    monkeypatch,
) -> None:
    """Lease waits fall back to a slower tail before returning ACK-only."""
    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._instance = SimpleNamespace(attach_output=AsyncMock(return_value=None))
    sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep)

    result = await adapter._reattach_interrupt_output("sess-slow-unwind")

    assert result is None
    fast_count = _INTERRUPT_OUTPUT_ATTACH_RETRY_COUNT
    slow_count = _INTERRUPT_OUTPUT_ATTACH_SLOW_RETRY_COUNT
    assert [call.args[0] for call in sleep.await_args_list] == (
        [_INTERRUPT_OUTPUT_ATTACH_RETRY_INTERVAL_SECONDS] * fast_count
        + [_INTERRUPT_OUTPUT_ATTACH_SLOW_RETRY_INTERVAL_SECONDS] * slow_count
    )
    assert adapter._instance.attach_output.await_count == fast_count + slow_count


def test_plan_exit_fallback_content_follows_runtime_language() -> None:
    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._resolve_runtime_language = MagicMock(return_value="en")
    assert adapter._plan_exit_fallback_content().startswith("Plan approved.")
    adapter._resolve_runtime_language.return_value = "zh"
    assert adapter._plan_exit_fallback_content().startswith("计划已获批准")


@pytest.mark.asyncio
async def test_prepare_code_mode_chat_turn_uses_injected_agent_manager() -> None:
    injected = MagicMock()
    injected.get_agent = AsyncMock(return_value=MagicMock())
    injected.wait_for_session_prewarm = AsyncMock()
    default_manager = MagicMock()
    default_manager.get_agent = AsyncMock()
    default_manager.wait_for_session_prewarm = AsyncMock()

    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._agent_manager = default_manager
    request = _chat_request("sess_injected", mode="code.plan")

    mode, sub_mode, _agent = await _default._prepare_code_mode_chat_turn(
        _default_ctx(server, request),
        request,
        "officeclaw",
        agent_manager=injected,
    )

    assert mode == "code"
    assert sub_mode == "plan"
    injected.get_agent.assert_awaited_once()
    default_manager.get_agent.assert_not_called()


def test_agent_lookup_uses_plan_sub_mode_and_workspace_dir() -> None:
    from jiuwenswarm.server.runtime.agent_manager import AgentManager

    manager = AgentManager.__new__(AgentManager)
    manager.service_id = "default"
    manager.agent_id = "office"
    request = SimpleNamespace(
        params={"mode": "code.plan", "workspace_dir": "E:/workspace/demo-project"}
    )
    mode, sub_mode, project_dir = manager._agent_lookup_from_request(request)
    assert mode == "code"
    assert sub_mode == "plan"
    assert project_dir == "E:/workspace/demo-project"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "interrupt_source",
    ["permission_interrupt", "confirm_interrupt", "ask_user_interrupt"],
)
async def test_tenant_interrupt_continuation_restores_code_route_before_defaults(
    interrupt_source: str,
) -> None:
    """OfficeClaw approval replies omit mode/project_dir; restore both before lookup."""
    agent = MagicMock()
    manager = MagicMock()
    manager.get_agent = AsyncMock(return_value=agent)
    manager.wait_for_session_prewarm = AsyncMock()
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._agent_manager = manager
    request = AgentRequest(
        request_id="resume-wire-request",
        channel_id="officeclaw",
        session_id="sess_resume_code",
        req_method=ReqMethod.CHAT_SEND,
        params={
            "query": "",
            "source": interrupt_source,
            "request_id": "bash_call_1",
            "answers": [{"selected_options": ["本次允许"]}],
        },
    )
    metadata = {
        "mode": "code.normal",
        # Historical OfficeClaw sessions may contain this stale value.
        "work_mode": "work",
        "project_dir": "E:/workspace/original-project",
    }

    with patch(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        return_value=metadata,
    ), patch.object(
        _default,
        "_sync_chat_request_metadata",
        return_value=metadata["project_dir"],
    ):
        mode, sub_mode, selected = await _default._prepare_code_mode_chat_turn(
            _default_ctx(server, request),
            request,
            "officeclaw",
            agent_manager=manager,
        )

    assert (mode, sub_mode, selected) == ("code", "normal", agent)
    assert request.params["mode"] == "code.normal"
    assert request.params["project_dir"] == metadata["project_dir"]
    manager.get_agent.assert_awaited_once_with(
        channel_id="officeclaw",
        mode="code",
        project_dir=metadata["project_dir"],
        sub_mode="normal",
    )


@pytest.mark.asyncio
async def test_explicit_code_mode_wins_over_stale_stored_work_mode() -> None:
    agent = MagicMock()
    manager = MagicMock()
    manager.get_agent = AsyncMock(return_value=agent)
    manager.wait_for_session_prewarm = AsyncMock()
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._agent_manager = manager
    request = _chat_request(
        "sess_explicit_code",
        mode="code.normal",
    )
    request.channel_id = "officeclaw"

    with patch(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        return_value={"mode": "unknown", "work_mode": "work"},
    ), patch.object(
        _default,
        "_sync_chat_request_metadata",
        return_value=None,
    ):
        mode, sub_mode, _ = await _default._prepare_code_mode_chat_turn(
            _default_ctx(server, request),
            request,
            "officeclaw",
            agent_manager=manager,
        )

    assert (mode, sub_mode) == ("code", "normal")
    assert request.params["mode"] == "code.normal"


def test_agent_lookup_restores_project_dir_for_interrupt_continuation() -> None:
    from jiuwenswarm.server.runtime.agent_manager import AgentManager

    manager = AgentManager.__new__(AgentManager)
    manager.service_id = "default"
    manager.agent_id = "office"
    request = SimpleNamespace(
        session_id="sess_project_resume",
        params={
            "source": "ask_user_interrupt",
            "request_id": "ask_call_1",
            "answers": [{"selected_options": ["继续"]}],
        },
    )
    metadata = {
        "mode": "team",
        "project_dir": "E:/workspace/team-project",
    }

    with patch(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        return_value=metadata,
    ):
        mode, sub_mode, project_dir = manager._agent_lookup_from_request(request)

    assert (mode, sub_mode) == ("team", None)
    assert project_dir == metadata["project_dir"]
    assert request.params["mode"] == "team"
    assert request.params["project_dir"] == metadata["project_dir"]


@pytest.mark.asyncio
async def test_disconnect_cleanup_then_stale_plan_reentry_blocked_by_slug(monkeypatch) -> None:
    """After disconnect cleanup discards the plan-exited flag, a stale (non-explicit)
    normal→plan request must still be blocked by the checkpoint plan_slug fallback.

    This pins the invariant that ``_cleanup_client_disconnect_session_runtime``
    clearing ``_plan_exited_sessions`` in its ``finally`` does not let a stale
    plan re-entry slip past the flag guard: the persisted ``plan_slug`` remains
    the authoritative defense-in-depth.
    """
    session_id = "sess_stale_after_disconnect"

    plan_agent = MagicMock()
    plan_instance = MagicMock()
    plan_agent.get_instance.return_value = plan_instance
    # Non-chat callers now await ensure_instance(), which builds the root
    # DeepAgent on demand instead of relying on eager construction.
    plan_agent.ensure_instance = AsyncMock(return_value=plan_instance)
    plan_instance.card = SimpleNamespace(id="code-agent")
    # Plan was completed: mode is normal but a plan_slug is still on checkpoint.
    plan_state = SimpleNamespace(mode="normal", plan_slug="leftover-slug")
    plan_instance.load_state.return_value = SimpleNamespace(plan_mode=plan_state)

    session = MagicMock()
    create_session = MagicMock(return_value=session)

    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    _push_mock = AsyncMock()
    monkeypatch.setattr(_default, "_push_plan_mode_exited", _push_mock)
    # No plan_entry_source => this is a stale re-entry, not an explicit /plan.
    request = _chat_request(session_id, "go", mode="code.plan")

    # Simulate the disconnect-cleanup finally having discarded the flag.
    assert session_id not in agent_ws_server_module._plan_exited_sessions
    try:
        with patch(
            "openjiuwen.core.single_agent.create_agent_session",
            create_session,
        ):
            session.pre_run = AsyncMock()
            session.post_run = AsyncMock()
            restored = await _default._ensure_code_mode_state(
                _default_ctx(server, request),
                request, "code", "plan", plan_agent
            )
    finally:
        agent_ws_server_module._plan_exited_sessions.discard(session_id)

    # Blocked via plan_slug fallback: slug cleared, state saved, push sent,
    # request normalized back to code.normal, and no mode switch performed.
    assert restored is False
    assert plan_state.plan_slug is None
    plan_instance.save_state.assert_called_once()
    session.post_run.assert_awaited_once()
    _push_mock.assert_awaited_once()
    plan_instance.switch_mode.assert_not_called()
    assert request.params["mode"] == "code.normal"


@pytest.mark.asyncio
async def test_ensure_skips_for_team_sub_mode() -> None:
    """_ensure_code_mode_state returns False for team sub_mode."""
    agent = MagicMock()
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    request = _chat_request("sess_team", mode="code.team")

    restored = await _default._ensure_code_mode_state(_default_ctx(server, request), request, "code", "team", agent)
    assert restored is False


@pytest.mark.asyncio
async def test_prepare_chat_turn_skips_approval_for_interrupt_resume() -> None:
    """_prepare_code_mode_chat_turn works correctly for interrupt resume requests."""
    session_id = "sess_interrupt"

    agent = MagicMock()
    manager = MagicMock()
    manager.get_agent = AsyncMock(return_value=agent)
    manager.wait_for_session_prewarm = AsyncMock()

    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._agent_manager = manager
    server._resolve_code_language = MagicMock(return_value="cn")

    request = _chat_request(
        session_id,
        "",
        mode="code.plan",
        extra_params={
            "request_id": "tool_req_1",
            "answers": {"approved": True},
            "source": "confirm_interrupt",
        },
    )

    mode, sub_mode, _agent = await _default._prepare_code_mode_chat_turn(_default_ctx(server, request), request, "tui")

    assert mode == "code"
    assert sub_mode == "plan"
    manager.get_agent.assert_awaited_once()
    manager.wait_for_session_prewarm.assert_awaited_once_with(session_id)


@pytest.mark.asyncio
async def test_ensure_skips_for_agent_mode() -> None:
    """_ensure_code_mode_state returns False for non-code modes."""
    agent = MagicMock()
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    request = _chat_request("sess_agent", mode="agent.fast")

    restored = await _default._ensure_code_mode_state(_default_ctx(server, request), request, "agent", "fast", agent)
    assert restored is False
