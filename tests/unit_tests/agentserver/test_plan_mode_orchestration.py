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

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server import agent_ws_server as agent_ws_server_module
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer


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

    mode, sub_mode, resolved_agent = await server._prepare_code_mode_chat_turn(
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
        agent_ws_server_module,
        "_sync_chat_request_metadata",
        return_value="/tmp/code-project",
    ):
        mode, sub_mode, resolved_agent = await server._prepare_code_mode_chat_turn(
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
async def test_prepare_chat_without_mode_restores_locked_session_mode() -> None:
    """Heartbeat CHAT_SEND without mode continues in the original session mode."""
    agent = MagicMock()
    manager = MagicMock()
    manager.get_agent = AsyncMock(return_value=agent)
    manager.wait_for_session_prewarm = AsyncMock()
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._agent_manager = manager
    request = AgentRequest(
        request_id="heartbeat-run-1",
        channel_id="web",
        session_id="heartbeat-session",
        req_method=ReqMethod.CHAT_SEND,
        params={
            "query": "continue",
            "automation": {
                "kind": "heartbeat",
                "job_id": "hb-1",
                "run_id": "run-1",
            },
        },
        metadata={
            "automation": {
                "kind": "heartbeat",
                "job_id": "hb-1",
                "run_id": "run-1",
            }
        },
    )

    with (
        patch(
            "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
            return_value={"mode": "code.normal"},
        ),
        patch.object(
            agent_ws_server_module,
            "_sync_chat_request_metadata",
            return_value=None,
        ),
    ):
        mode, sub_mode, resolved = await server._prepare_code_mode_chat_turn(
            request, "web"
        )

    assert (mode, sub_mode, resolved) == ("code", "normal", agent)
    assert request.params["mode"] == "code.normal"
    assert request.params["work_mode"] == "code"
    manager.get_agent.assert_awaited_once_with(
        channel_id="web",
        mode="code",
        project_dir=None,
        sub_mode="normal",
    )


@pytest.mark.asyncio
async def test_prepare_heartbeat_chat_without_mode_restores_locked_team_session() -> None:
    """A mode-less Heartbeat must re-enter the Session's Team runtime."""
    agent = MagicMock()
    manager = MagicMock()
    manager.get_agent = AsyncMock(return_value=agent)
    manager.wait_for_session_prewarm = AsyncMock()
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._agent_manager = manager
    request = AgentRequest(
        request_id="heartbeat-team-run",
        channel_id="web",
        session_id="heartbeat-team-session",
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "continue the team task"},
        metadata={
            "automation": {
                "kind": "heartbeat",
                "job_id": "hb-team",
                "run_id": "heartbeat-team-run",
            }
        },
    )

    with (
        patch(
            "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
            return_value={"mode": "team.work.normal", "work_mode": "work"},
        ),
        patch.object(
            agent_ws_server_module,
            "_sync_chat_request_metadata",
            return_value=None,
        ) as sync_metadata,
    ):
        mode, sub_mode, resolved = await server._prepare_code_mode_chat_turn(
            request, "web"
        )

    assert (mode, sub_mode, resolved) == ("team", None, agent)
    assert request.params["mode"] == "team.work.normal"
    assert request.params["work_mode"] == "work"
    manager.get_agent.assert_awaited_once_with(
        channel_id="web",
        mode="team",
        project_dir=None,
        sub_mode=None,
    )
    assert sync_metadata.call_args.kwargs["explicit_mode_provided"] is False


@pytest.mark.asyncio
async def test_prepare_chat_without_mode_restores_locked_work_session_on_tui() -> None:
    """A legacy work session must not inherit TUI's code default on resume."""
    agent = MagicMock()
    manager = MagicMock()
    manager.get_agent = AsyncMock(return_value=agent)
    manager.wait_for_session_prewarm = AsyncMock()
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._agent_manager = manager
    request = AgentRequest(
        request_id="heartbeat-work-run",
        channel_id="tui",
        session_id="heartbeat-work-session",
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "continue work session"},
        metadata={
            "automation": {
                "kind": "heartbeat",
                "job_id": "hb-work",
                "run_id": "heartbeat-work-run",
            }
        },
    )

    with (
        patch(
            "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
            return_value={"mode": "agent"},
        ),
        patch.object(
            agent_ws_server_module,
            "_sync_chat_request_metadata",
            return_value=None,
        ),
    ):
        mode, sub_mode, resolved = await server._prepare_code_mode_chat_turn(
            request, "tui"
        )

    assert (mode, sub_mode, resolved) == ("agent", None, agent)
    assert request.params["mode"] == "agent"
    assert request.params["work_mode"] == "work"
    manager.get_agent.assert_awaited_once_with(
        channel_id="tui",
        mode="agent",
        project_dir=None,
        sub_mode=None,
    )


@pytest.mark.asyncio
async def test_prepare_chat_uses_locked_persist_session_metadata() -> None:
    agent = MagicMock()
    manager = MagicMock()
    manager.get_agent = AsyncMock(return_value=agent)
    manager.wait_for_session_prewarm = AsyncMock()
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._agent_manager = manager
    request = _chat_request(
        "sess_persist_locked",
        mode="agent",
        extra_params={
            "persist_session": False,
            "eternal_conversation_enabled": False,
        },
    )
    metadata = {"work_mode": "work", "persist_session": True}

    with patch(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        return_value=metadata,
    ), patch.object(
        agent_ws_server_module,
        "_sync_chat_request_metadata",
        return_value="",
    ):
        await server._prepare_code_mode_chat_turn(request, "web")

    assert "persist_session" not in request.params
    assert request.params["eternal_conversation_enabled"] is True


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
        agent_ws_server_module,
        "_sync_chat_request_metadata",
        return_value=" /tmp/locked-project ",
    ):
        mode, sub_mode, resolved_agent = await server._prepare_code_mode_chat_turn(
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
    commit = AsyncMock()

    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    request = _chat_request(session_id, "hello", mode="code.normal")

    with patch(
        "openjiuwen.core.single_agent.create_agent_session",
        create_session,
    ):
        session.pre_run = pre_run
        # 落盘走 commit 而不是 post_run：这条路径也可能拿到正在跑的那个 session，
        # post_run 会把它关掉。
        session.commit = commit
        restored = await server._ensure_code_mode_state(
            request, "code", "normal", plan_agent
        )

    assert restored is True
    plan_instance.switch_mode.assert_called_once_with(session=session, mode="normal")
    commit.assert_awaited_once()


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
        session.commit = AsyncMock()
        restored = await server._ensure_code_mode_state(
            request, "code", "plan", plan_agent
        )

    assert restored is False
    plan_instance.switch_mode.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_code_mode_state_allows_explicit_plan_reentry_after_exit() -> None:
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
    server._push_plan_mode_exited = AsyncMock()
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
            session.commit = AsyncMock()
            restored = await server._ensure_code_mode_state(
                request, "code", "plan", plan_agent
            )
    finally:
        agent_ws_server_module._plan_exited_sessions.discard(session_id)

    assert restored is False
    plan_instance.switch_mode.assert_called_once_with(session=session, mode="plan")
    server._push_plan_mode_exited.assert_not_awaited()
    assert request.params["mode"] == "code.plan"


@pytest.mark.asyncio
async def test_disconnect_cleanup_then_stale_plan_reentry_blocked_by_slug() -> None:
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
    server._push_plan_mode_exited = AsyncMock()
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
            session.commit = AsyncMock()
            restored = await server._ensure_code_mode_state(
                request, "code", "plan", plan_agent
            )
    finally:
        agent_ws_server_module._plan_exited_sessions.discard(session_id)

    # Blocked via plan_slug fallback: slug cleared, state saved, push sent,
    # request normalized back to code.normal, and no mode switch performed.
    assert restored is False
    assert plan_state.plan_slug is None
    plan_instance.save_state.assert_called_once()
    session.commit.assert_awaited_once()
    server._push_plan_mode_exited.assert_awaited_once()
    plan_instance.switch_mode.assert_not_called()
    assert request.params["mode"] == "code.normal"


@pytest.mark.asyncio
async def test_plain_work_turn_skips_sync_without_touching_the_agent() -> None:
    """work 的准入面覆盖 IM / cron / CLI / Web work 的每条普通消息。

    这些会话绝大多数从未开过 Plan，不该为了同步 plan 状态去建 root DeepAgent
    （重跑工具注册、rail 装配、MCP 注册）。
    """
    agent = MagicMock()
    agent.ensure_instance = AsyncMock()
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    request = _chat_request(
        "sess_plain_work", mode="agent", extra_params={"work_mode": "work"}
    )

    restored = await server._ensure_code_mode_state(request, "agent", None, agent)

    assert restored is False
    agent.ensure_instance.assert_not_awaited()
    agent.get_live_session_instance.assert_not_called()


@pytest.mark.asyncio
async def test_work_turn_with_previous_plan_mode_still_syncs() -> None:
    """上一轮停在 plan 的会话（含服务重启后）必须被切回普通模式。"""
    session_id = "sess_work_restart"

    plan_agent = MagicMock()
    plan_instance = MagicMock()
    plan_agent.get_live_session_instance.return_value = None
    plan_agent.ensure_instance = AsyncMock(return_value=plan_instance)
    plan_instance.card = SimpleNamespace(id="deep-agent")
    plan_instance.load_state.return_value = SimpleNamespace(
        plan_mode=SimpleNamespace(mode="plan", plan_slug="slug")
    )

    session = MagicMock()
    session.pre_run = AsyncMock()
    session.commit = AsyncMock()

    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._push_plan_mode_exited = AsyncMock()
    request = _chat_request(
        session_id,
        "继续",
        mode="agent",
        extra_params={
            "work_mode": "work",
            agent_ws_server_module._SESSION_PREVIOUS_MODE_KEY: "agent.plan",
        },
    )

    with patch(
        "openjiuwen.core.single_agent.create_agent_session",
        MagicMock(return_value=session),
    ):
        restored = await server._ensure_code_mode_state(request, "agent", None, plan_agent)

    assert restored is True
    plan_instance.switch_mode.assert_called_once_with(session=session, mode="normal")


@pytest.mark.asyncio
async def test_plan_mode_exited_push_uses_the_session_profile_mode() -> None:
    """work 会话不能收到写死的 ``code.normal``——TUI 会消费这个字段。"""
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server.send_push = AsyncMock()
    request = _chat_request(
        "sess_work_exit", mode="agent", extra_params={"work_mode": "work"}
    )

    await server._push_plan_mode_exited(request)

    pushed = server.send_push.await_args.args[0]
    assert pushed["payload"]["mode"] == "agent"


@pytest.mark.asyncio
async def test_internal_heartbeat_pushes_visible_prompt_before_stream() -> None:
    automation = {
        "kind": "heartbeat",
        "job_id": "hb-1",
        "run_id": "run-1",
        "triggered_at": 123.0,
    }

    async def response_stream():
        yield AgentResponseChunk(
            request_id="run-1",
            channel_id="web",
            payload={
                "event_type": "chat.processing_status",
                "is_processing": True,
                "is_complete": False,
            },
            is_complete=False,
        )
        yield AgentResponseChunk(
            request_id="run-1",
            channel_id="web",
            payload={"event_type": "chat.final", "content": "apple"},
            is_complete=False,
        )

    agent = MagicMock()
    agent.process_message_stream.return_value = response_stream()
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._heartbeat_runtime = SimpleNamespace(retain_agent=MagicMock())
    server._prepare_code_mode_chat_turn = AsyncMock(return_value=("agent", None, agent))
    server._ensure_code_mode_state = AsyncMock(return_value=False)
    server._check_post_process_plan_exit = AsyncMock()
    server.send_push = AsyncMock()
    request = AgentRequest(
        request_id="run-1",
        channel_id="web",
        session_id="sess-1",
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "say a fruit", "content": "say a fruit", "automation": automation},
        metadata={"automation": automation},
    )

    with patch(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep.ensure_persistent_checkpointer",
        AsyncMock(),
    ):
        await server.execute_internal_heartbeat(request)

    pushes = [call.args[0] for call in server.send_push.await_args_list]
    assert pushes[0]["payload"] == {
        "event_type": "chat.processing_status",
        "session_id": "sess-1",
        "is_processing": True,
        "is_complete": False,
        "content": "say a fruit",
    }
    assert pushes[0]["metadata"] == {"automation": automation}
    assert pushes[1]["payload"]["content"] == "say a fruit"
    assert pushes[2]["payload"]["content"] == "apple"
    assert pushes[-1]["payload"] == {
        "event_type": "chat.processing_status",
        "session_id": "sess-1",
        "is_processing": False,
        "is_complete": True,
    }


@pytest.mark.asyncio
async def test_internal_heartbeat_cancel_closes_processing_status() -> None:
    automation = {
        "kind": "heartbeat",
        "job_id": "hb-1",
        "run_id": "run-cancelled",
        "triggered_at": 123.0,
    }

    async def response_stream():
        yield AgentResponseChunk(
            request_id="run-cancelled",
            channel_id="web",
            payload={
                "event_type": "chat.processing_status",
                "is_processing": True,
                "is_complete": False,
            },
            is_complete=False,
        )
        raise asyncio.CancelledError

    agent = MagicMock()
    agent.process_message_stream.return_value = response_stream()
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._heartbeat_runtime = SimpleNamespace(retain_agent=MagicMock())
    server._prepare_code_mode_chat_turn = AsyncMock(return_value=("agent", None, agent))
    server._ensure_code_mode_state = AsyncMock(return_value=False)
    server._check_post_process_plan_exit = AsyncMock()
    server.send_push = AsyncMock()
    request = AgentRequest(
        request_id="run-cancelled",
        channel_id="web",
        session_id="sess-1",
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "say a fruit", "content": "say a fruit", "automation": automation},
        metadata={"automation": automation},
    )

    with patch(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep.ensure_persistent_checkpointer",
        AsyncMock(),
    ), pytest.raises(asyncio.CancelledError):
        await server.execute_internal_heartbeat(request)

    pushes = [call.args[0] for call in server.send_push.await_args_list]
    assert pushes[-1]["payload"] == {
        "event_type": "chat.processing_status",
        "session_id": "sess-1",
        "is_processing": False,
        "is_complete": True,
    }
    assert pushes[-1]["metadata"] == {"automation": automation}


@pytest.mark.asyncio
async def test_ensure_skips_for_team_sub_mode() -> None:
    """_ensure_code_mode_state returns False for team sub_mode."""
    agent = MagicMock()
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    request = _chat_request("sess_team", mode="code.team")

    restored = await server._ensure_code_mode_state(request, "code", "team", agent)
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

    mode, sub_mode, _agent = await server._prepare_code_mode_chat_turn(request, "tui")

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

    restored = await server._ensure_code_mode_state(request, "agent", "fast", agent)
    assert restored is False
