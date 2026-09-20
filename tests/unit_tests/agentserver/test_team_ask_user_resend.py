# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""team 模式 ask_user 链路的看门狗与控制续接判定测试。

覆盖两块防御逻辑:
1. 提问卡重发看门狗——卡片投递丢失时周期重发,任何进度 chunk 或流结束
   都必须停掉重发,不能让任务泄漏到轮次之外。
2. ``is_team_control_continuation`` 谓词——识别 ask_user/permission 作答的
   控制续接请求(此类请求跳过 request-scoped MCP 注册,避免与被中断的
   原始请求死锁)。
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace

import pytest

from jiuwenswarm.server.runtime.agent_adapter import team_helpers


def _interactive_input():
    from openjiuwen.core.session.interaction.interactive_input import InteractiveInput

    interactive_input = InteractiveInput()
    interactive_input.update(
        "call_ask_user_1",
        {"status": "answered", "answers": [{"selected_options": ["当前项目目录"]}]},
    )
    return interactive_input


def _ask_user_card(request_id: str = "call_ask_user_1") -> dict:
    return {
        "event_type": "chat.ask_user_question",
        "request_id": request_id,
        "questions": [
            {
                "question": "文件存放到哪个目录?",
                "header": "Question",
                "options": [],
                "multi_select": False,
            }
        ],
        "source": "ask_user_interrupt",
    }


# --- is_team_control_continuation 谓词 ---


@pytest.mark.parametrize("mode", ["team", "team.plan", "code.team"])
def test_is_team_control_continuation_accepts_team_modes(mode: str) -> None:
    request = SimpleNamespace(params={"mode": mode, "source": "ask_user_interrupt"})
    assert team_helpers.is_team_control_continuation(request, _interactive_input()) is True


def test_is_team_control_continuation_accepts_permission_source() -> None:
    request = SimpleNamespace(params={"mode": "team", "source": "permission_interrupt"})
    assert team_helpers.is_team_control_continuation(request, _interactive_input()) is True


def test_is_team_control_continuation_rejects_agent_mode() -> None:
    request = SimpleNamespace(params={"mode": "agent", "source": "ask_user_interrupt"})
    assert team_helpers.is_team_control_continuation(request, _interactive_input()) is False


def test_is_team_control_continuation_defaults_missing_mode_to_agent() -> None:
    request = SimpleNamespace(params={"source": "ask_user_interrupt"})
    assert team_helpers.is_team_control_continuation(request, _interactive_input()) is False


def test_is_team_control_continuation_rejects_other_sources() -> None:
    request = SimpleNamespace(params={"mode": "team", "source": "chat"})
    assert team_helpers.is_team_control_continuation(request, _interactive_input()) is False


def test_is_team_control_continuation_requires_interactive_input_query() -> None:
    request = SimpleNamespace(params={"mode": "team", "source": "ask_user_interrupt"})
    assert team_helpers.is_team_control_continuation(request, "普通文本消息") is False


def test_is_team_control_continuation_tolerates_non_dict_params() -> None:
    request = SimpleNamespace(params=None)
    assert team_helpers.is_team_control_continuation(request, _interactive_input()) is False


# --- 重发看门狗单元 ---


def test_progress_chunk_types_cover_progress_but_not_usage() -> None:
    # llm_usage 伴随 interaction chunk 本身到达,不算轮次推进,
    # 不得取消看门狗。
    assert "llm_usage" not in team_helpers._TEAM_PROGRESS_CHUNK_TYPES
    for chunk_type in (
        "llm_output",
        "llm_reasoning",
        "content_chunk",
        "tool_call",
        "tool_update",
        "controller_output",
        "answer",
        "__interaction__",
        "message",
        "team.runtime_ready",
        "team.completed",
    ):
        assert chunk_type in team_helpers._TEAM_PROGRESS_CHUNK_TYPES


@pytest.mark.asyncio
async def test_resend_ask_user_loop_broadcasts_copies_up_to_max(monkeypatch) -> None:
    broadcasts: list[dict] = []
    monkeypatch.setattr(
        team_helpers, "_broadcast_event", lambda c, s, e: broadcasts.append(e)
    )
    monkeypatch.setattr(team_helpers, "_ASK_USER_RESEND_INTERVAL_S", 0.01)
    monkeypatch.setattr(team_helpers, "_ASK_USER_RESEND_MAX", 3)
    event = _ask_user_card("rid-1")

    await team_helpers._resend_ask_user_loop("web", "sess", event)

    assert len(broadcasts) == 3
    assert all(item is not event for item in broadcasts)
    assert all(item["request_id"] == "rid-1" for item in broadcasts)


@pytest.mark.asyncio
async def test_resend_ask_user_loop_cancelled_stops_broadcasting(monkeypatch) -> None:
    broadcasts: list[dict] = []
    monkeypatch.setattr(
        team_helpers, "_broadcast_event", lambda c, s, e: broadcasts.append(e)
    )
    monkeypatch.setattr(team_helpers, "_ASK_USER_RESEND_INTERVAL_S", 0.02)
    monkeypatch.setattr(team_helpers, "_ASK_USER_RESEND_MAX", 10)

    task = asyncio.create_task(
        team_helpers._resend_ask_user_loop("web", "sess", _ask_user_card("rid-1"))
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    count = len(broadcasts)
    assert 1 <= count <= 3
    await asyncio.sleep(0.1)
    assert len(broadcasts) == count


@pytest.mark.asyncio
async def test_arm_ask_user_resend_replaces_existing_and_cancel_clears(monkeypatch) -> None:
    started: list[str] = []

    async def _fake_loop(channel_id, session_id, event) -> None:
        started.append(str(event.get("request_id")))
        await asyncio.Event().wait()

    monkeypatch.setattr(team_helpers, "_resend_ask_user_loop", _fake_loop)
    pending: dict = {}

    team_helpers._arm_ask_user_resend(pending, "web", "sess", _ask_user_card("rid-1"))
    first = pending["rid-1"]
    await asyncio.sleep(0.01)
    assert started == ["rid-1"]

    team_helpers._arm_ask_user_resend(pending, "web", "sess", _ask_user_card("rid-1"))
    second = pending["rid-1"]
    await asyncio.sleep(0.01)
    assert first.cancelled()
    assert second is not first
    assert started == ["rid-1", "rid-1"]

    team_helpers._cancel_ask_user_resends(pending)
    await asyncio.sleep(0.01)
    assert pending == {}
    assert second.cancelled()


@pytest.mark.asyncio
async def test_arm_ask_user_resend_skips_without_request_id(monkeypatch) -> None:
    async def _fake_loop(channel_id, session_id, event) -> None:
        raise AssertionError("must not start without request_id")

    monkeypatch.setattr(team_helpers, "_resend_ask_user_loop", _fake_loop)
    pending: dict = {}

    team_helpers._arm_ask_user_resend(
        pending, "web", "sess", _ask_user_card("")
    )

    assert pending == {}


# --- _consume_stream_with_query 集成 ---


class _ResendTestManager:
    def __init__(self) -> None:
        self._seen: dict[str, bool] = {}

    def reset_seen_team_events(self, session_id: str) -> None:
        self._seen.pop(session_id, None)

    def reset_round_complete(self, session_id: str) -> None:
        pass

    def reset_workflow_completed(self, session_id: str) -> None:
        pass

    def get_monitor_handler(self, session_id: str):
        return None

    def clear_pending_runtime(self, session_id: str) -> None:
        pass

    def clear_active_runtime(self, session_id: str) -> None:
        pass

    def pop_stream_task(self, session_id: str):
        pass


async def _run_consume(monkeypatch, stream_factory, parse_fn, *, session_id: str = "sess-resend") -> list[dict]:
    broadcasts: list[dict] = []
    monkeypatch.setattr(
        team_helpers, "_broadcast_event", lambda c, s, e: broadcasts.append(e)
    )
    monkeypatch.setattr(team_helpers, "_ASK_USER_RESEND_INTERVAL_S", 0.05)
    monkeypatch.setattr(team_helpers, "_ASK_USER_RESEND_MAX", 5)
    monkeypatch.setattr(team_helpers, "_run_agent_team_streaming", stream_factory)
    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel_id: _ResendTestManager())
    monkeypatch.setattr(team_helpers, "parse_stream_chunk", parse_fn)

    await team_helpers._consume_stream_with_query(
        "web",
        session_id,
        SimpleNamespace(team_name="spec-team"),
        "生成一份包含杭州天气的文件",
        round_id=1,
    )
    return broadcasts


@pytest.mark.asyncio
async def test_consume_stream_arms_resend_and_cancels_on_progress(monkeypatch) -> None:
    """提问卡广播后进入静默期→看门狗重发;进度 chunk 到达→停止重发。"""
    ask_chunk = SimpleNamespace(type="controller_output", payload={}, role=None)
    progress_chunk = SimpleNamespace(
        type="llm_output", payload={"content": "继续"}, role=None
    )

    def _fake_parse(chunk):
        if chunk is ask_chunk:
            return _ask_user_card()
        return {"event_type": "chat.delta", "content": "继续"}

    async def _fake_stream(**kwargs):
        yield ask_chunk
        await asyncio.sleep(0.12)  # 静默期:两次重发(0.05/0.10)已发,第三次未到
        yield progress_chunk

    broadcasts = await _run_consume(monkeypatch, _fake_stream, _fake_parse)

    ask_events = [e for e in broadcasts if e.get("event_type") == "chat.ask_user_question"]
    # 原始卡片 1 张 + 静默期内重发 2 次;进度 chunk 到达后取消,重发 3-5 不再发出
    assert len(ask_events) == 3
    assert all(e["request_id"] == "call_ask_user_1" for e in ask_events)
    assert all(e.get("session_id") == "sess-resend" for e in ask_events)

    deltas = [e for e in broadcasts if e.get("event_type") == "chat.delta"]
    assert len(deltas) == 1
    assert broadcasts.index(deltas[0]) > broadcasts.index(ask_events[-1])

    # 轮次结束后不得再有重发漏出
    await asyncio.sleep(0.25)
    assert len(ask_events) == 3


@pytest.mark.asyncio
async def test_consume_stream_cancels_resends_on_stream_end(monkeypatch) -> None:
    """流结束(用户取消/团队收尾)时,未作答卡片的重发任务必须一并取消。"""
    ask_chunk = SimpleNamespace(type="controller_output", payload={}, role=None)

    def _fake_parse(chunk):
        return _ask_user_card()

    async def _fake_stream(**kwargs):
        yield ask_chunk

    broadcasts = await _run_consume(monkeypatch, _fake_stream, _fake_parse)

    ask_events = [e for e in broadcasts if e.get("event_type") == "chat.ask_user_question"]
    assert len(ask_events) == 1

    await asyncio.sleep(0.2)
    ask_events = [e for e in broadcasts if e.get("event_type") == "chat.ask_user_question"]
    assert len(ask_events) == 1
