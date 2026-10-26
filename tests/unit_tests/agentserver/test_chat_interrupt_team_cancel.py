# coding: utf-8
"""chat.interrupt 对 team mode 必须镜像 canonical _process_team_interrupt
（interface.py:2107-2168）：cancel→cancel_session_runtime（真停 team run driver），
pause→pause_session_runtime，resume→仅消息，supplement/未知→"暂不支持"。
而非依赖 agent_manager（team mode 无 agent → "no existing agent" 早退 →
原语永不触达，team run driver 不停）。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod


def _team_interrupt_request(*, session_id="sess-1", intent="cancel",
                            request_id="interrupt-1") -> AgentRequest:
    return AgentRequest(
        request_id=request_id,
        channel_id="web",
        session_id=session_id,
        req_method=ReqMethod.CHAT_CANCEL,
        params={"intent": intent, "mode": "team", "team_name": "oc_team_t",
                "request_id": "run-1"},
        is_stream=False,
    )


def _ctx(request, *, agent_nowait=None):
    """RequestContext stub: _handle_cancel only touches ctx.request /
    ctx.services.agent_manager / ctx.sink."""
    services = SimpleNamespace(
        agent_manager=SimpleNamespace(
            get_agent_nowait=MagicMock(return_value=agent_nowait),
            get_agent=AsyncMock(return_value=agent_nowait),
            get_client_capabilities=MagicMock(return_value={}),
        ),
        session_stream_tasks={request.session_id or "default": {}},
    )
    sink = AsyncMock()
    return SimpleNamespace(request=request, services=services, sink=sink,
                          connection_id="c1")


@pytest.fixture
def fake_team_manager(monkeypatch):
    """Spy 原语；cancel 第一次返回 True 并清空 has_task，第二次返回 False（幂等）。"""
    state = {"has_task": True, "cancel_called": False, "pause_called": False}

    async def _cancel(session_id, reason=""):
        state["cancel_called"] = True
        if state["has_task"]:
            state["has_task"] = False
            return True
        return False

    async def _pause(session_id, reason=""):
        state["pause_called"] = True
        return True

    tm = SimpleNamespace(
        cancel_session_runtime=AsyncMock(side_effect=_cancel),
        pause_session_runtime=AsyncMock(side_effect=_pause),
        has_stream_task=lambda sid: state["has_task"],
    )
    import jiuwenswarm.agents.harness.team as team_pkg
    monkeypatch.setattr(team_pkg, "get_team_manager", lambda channel_id: tm)
    return tm, state


@pytest.mark.asyncio
async def test_team_cancel_cancels_team_run_driver(fake_team_manager):
    """RED: team mode + cancel 必须调 cancel_session_runtime 取消 team run driver。"""
    from jiuwenswarm.server.handlers.chat import _handle_cancel
    tm, state = fake_team_manager
    req = _team_interrupt_request(session_id="sess-1", intent="cancel")
    resp = await _handle_cancel(_ctx(req), allow_create=False, send_response=False)

    tm.cancel_session_runtime.assert_awaited_once()
    assert tm.cancel_session_runtime.await_args.args[0] == "sess-1"
    tm.pause_session_runtime.assert_not_awaited()
    assert resp.payload["event_type"] == "chat.interrupt_result"
    assert resp.payload["success"] is True
    assert resp.payload["intent"] == "cancel"
    assert state["has_task"] is False


@pytest.mark.asyncio
async def test_team_pause_calls_pause_not_cancel(fake_team_manager):
    """team mode + pause → 调 pause_session_runtime，cancel_session_runtime 不被调。"""
    from jiuwenswarm.server.handlers.chat import _handle_cancel
    tm, _ = fake_team_manager
    req = _team_interrupt_request(session_id="sess-1", intent="pause")
    resp = await _handle_cancel(_ctx(req), allow_create=False, send_response=False)
    tm.pause_session_runtime.assert_awaited_once()
    tm.cancel_session_runtime.assert_not_awaited()
    assert resp.payload["intent"] == "pause"
    assert resp.payload["success"] is True
    assert resp.payload["message"] == "团队已暂停"


@pytest.mark.asyncio
async def test_team_resume_no_runtime_action(fake_team_manager):
    """team mode + resume → 两原语均不被调，消息含"直接发送下一条消息"。"""
    from jiuwenswarm.server.handlers.chat import _handle_cancel
    tm, _ = fake_team_manager
    req = _team_interrupt_request(session_id="sess-1", intent="resume")
    resp = await _handle_cancel(_ctx(req), allow_create=False, send_response=False)
    tm.cancel_session_runtime.assert_not_awaited()
    tm.pause_session_runtime.assert_not_awaited()
    assert resp.payload["intent"] == "resume"
    assert resp.payload["success"] is True
    assert "直接发送下一条消息" in resp.payload["message"]


@pytest.mark.asyncio
async def test_team_supplement_not_supported_no_cancel(fake_team_manager):
    """team mode + supplement → 镜像 canonical: 不取消，success=False "暂不支持"。"""
    from jiuwenswarm.server.handlers.chat import _handle_cancel
    tm, _ = fake_team_manager
    req = _team_interrupt_request(session_id="sess-1", intent="supplement")
    resp = await _handle_cancel(_ctx(req), allow_create=False, send_response=False)
    tm.cancel_session_runtime.assert_not_awaited()
    tm.pause_session_runtime.assert_not_awaited()
    assert resp.payload["success"] is False
    assert "暂不支持" in resp.payload["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_error", [None, TimeoutError, RuntimeError, asyncio.CancelledError])
async def test_team_cancel_no_stream_task_returns_false(monkeypatch, stop_error):
    """Distinguish absent runtimes from failed stops; propagate caller cancellation."""
    import jiuwenswarm.agents.harness.team as team_pkg
    tm = SimpleNamespace(
        cancel_session_runtime=AsyncMock(return_value=False, side_effect=stop_error),
        pause_session_runtime=AsyncMock(return_value=False),
        has_stream_task=lambda sid: False,
    )
    monkeypatch.setattr(team_pkg, "get_team_manager", lambda channel_id: tm)
    from jiuwenswarm.server.handlers.chat import _handle_cancel
    req = _team_interrupt_request(session_id="sess-empty", intent="cancel")
    ctx = _ctx(req)
    if stop_error is asyncio.CancelledError:
        with pytest.raises(asyncio.CancelledError):
            await _handle_cancel(ctx, allow_create=False, send_response=True)
        ctx.sink.send_wire.assert_not_awaited()
        return
    resp = await _handle_cancel(ctx, allow_create=False, send_response=True)
    assert resp.ok is True
    assert resp.payload["event_type"] == "chat.interrupt_result"
    assert resp.payload["success"] is False
    assert resp.payload["intent"] == "cancel"
    expected = "团队停止未完成，请稍后重试" if stop_error else "当前没有可取消的团队任务"
    assert resp.payload["message"] == expected
    ctx.sink.send_wire.assert_awaited_once()
    assert ctx.sink.send_wire.await_args.args[0]["body"]["result"] == resp.payload


@pytest.mark.asyncio
async def test_team_cancel_idempotent_second_returns_false(fake_team_manager):
    """第二次 cancel：team run driver 已被第一次取消 → success=False。"""
    from jiuwenswarm.server.handlers.chat import _handle_cancel
    tm, _ = fake_team_manager
    req = _team_interrupt_request(session_id="sess-1", intent="cancel")
    first = await _handle_cancel(_ctx(req), allow_create=False, send_response=False)
    req2 = _team_interrupt_request(session_id="sess-1", intent="cancel",
                                   request_id="interrupt-2")
    second = await _handle_cancel(_ctx(req2), allow_create=False, send_response=False)
    assert first.payload["success"] is True
    assert second.payload["success"] is False
    assert "没有可取消" in second.payload["message"]


@pytest.mark.asyncio
async def test_non_team_mode_skips_shortcut(monkeypatch):
    """非 team params → 不进短路 → 走 agent_manager 既有路径（process_message）。"""
    import jiuwenswarm.agents.harness.team as team_pkg
    tm = SimpleNamespace(
        cancel_session_runtime=AsyncMock(return_value=True),
        pause_session_runtime=AsyncMock(return_value=True),
    )
    monkeypatch.setattr(team_pkg, "get_team_manager", lambda channel_id: tm)
    from jiuwenswarm.server.handlers.chat import _handle_cancel
    stub_resp = SimpleNamespace(
        ok=True, payload={"event_type": "chat.interrupt_result", "success": True,
                          "message": "ok"},
        request_id="interrupt-1", channel_id="web", metadata={},
    )
    stub_agent = SimpleNamespace(process_message=AsyncMock(return_value=stub_resp))
    req = AgentRequest(request_id="interrupt-1", channel_id="web", session_id="sess-1",
                       req_method=ReqMethod.CHAT_CANCEL,
                       params={"intent": "cancel", "mode": "agent"}, is_stream=False)
    resp = await _handle_cancel(_ctx(req, agent_nowait=stub_agent),
                                allow_create=False, send_response=False)
    tm.cancel_session_runtime.assert_not_awaited()
    tm.pause_session_runtime.assert_not_awaited()
    stub_agent.process_message.assert_awaited_once()
    assert resp is stub_resp
