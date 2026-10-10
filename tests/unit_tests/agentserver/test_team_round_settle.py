"""回归测试：零任务团队问答轮的收尾补判。

修复前：team.completed 门禁要求任务行存在（is_team_completed 条件①），
消息制委派/纯问答团从不建任务行；而 seen_team_events 又被回合开始的成员
状态帧置位 → chat.final 的 processing_status 收尾被抑制 → 零任务轮没有任何
终态帧，长寿命流永挂，前端永久「正在思考」。

修复后：leader chat.final 到达且 should_finish_round 为 False 时，按 settle
三件套补判（零在途成员 + 零非终态任务 + 零未读消息）——落定即照常发
processing_status 终态；读数不可得一律按「未落定」退化（不补终态）。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from openjiuwen.agent_teams.schema.team import TeamRole

from jiuwenswarm.server.runtime.agent_adapter import team_helpers, team_stall_watchdog


# ------------------------------------------------------------------
# _team_round_settled 单元测试
# ------------------------------------------------------------------


def _patch_progress(monkeypatch: pytest.MonkeyPatch, value):
    async def _progress(_cid, _sid):
        return value

    # _team_round_settled 在函数体内惰性 import team_progress_snapshot
    monkeypatch.setattr(team_stall_watchdog, "team_progress_snapshot", _progress)


def _patch_unread(monkeypatch: pytest.MonkeyPatch, value):
    async def _unread(_cid, _sid):
        return value

    monkeypatch.setattr(team_helpers, "_team_has_unread_messages", _unread)


@pytest.mark.asyncio
async def test_settled_when_no_inflight_no_pending_no_unread(monkeypatch) -> None:
    """零在途 + 零非终态任务 + 零未读 → 落定。"""
    _patch_progress(monkeypatch, (0, 0, False))
    _patch_unread(monkeypatch, False)
    assert await team_helpers._team_round_settled("desktop", "s1") is True


@pytest.mark.asyncio
async def test_not_settled_when_member_in_flight(monkeypatch) -> None:
    """成员在途（leader 先答、成员后报的回合）→ 未落定，不补终态。"""
    _patch_progress(monkeypatch, (1, 0, False))
    _patch_unread(monkeypatch, False)
    assert await team_helpers._team_round_settled("desktop", "s1") is False


@pytest.mark.asyncio
async def test_not_settled_when_tasks_pending(monkeypatch) -> None:
    """有非终态任务 → 未落定（走 team.completed 旧路径）。"""
    _patch_progress(monkeypatch, (0, 2, False))
    _patch_unread(monkeypatch, False)
    assert await team_helpers._team_round_settled("desktop", "s1") is False


@pytest.mark.asyncio
async def test_not_settled_when_unread_messages(monkeypatch) -> None:
    """有未读消息（成员回报未消化）→ 未落定。"""
    _patch_progress(monkeypatch, (0, 0, False))
    _patch_unread(monkeypatch, True)
    assert await team_helpers._team_round_settled("desktop", "s1") is False


@pytest.mark.asyncio
async def test_unknown_when_progress_unavailable(monkeypatch) -> None:
    """快照不可得 → 返回 None（未知）：调用方续窗重试而非一次性放弃。"""
    _patch_progress(monkeypatch, None)
    _patch_unread(monkeypatch, False)
    assert await team_helpers._team_round_settled("desktop", "s1") is None


@pytest.mark.asyncio
async def test_unknown_when_unread_unavailable(monkeypatch) -> None:
    """未读消息读数不可得 → 返回 None（未知）：调用方续窗重试。"""
    _patch_progress(monkeypatch, (0, 0, False))
    _patch_unread(monkeypatch, None)
    assert await team_helpers._team_round_settled("desktop", "s1") is None


# ------------------------------------------------------------------
# chat.final 分支集成测试（复用 test_team_helpers 的最小 fake 风格）
# ------------------------------------------------------------------


class _SettleRecordingManager:
    """最小 TeamManager fake：记录广播 + seen_team_events 三件套。"""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self._seen: dict[str, bool] = {}
        self._terminal: dict[str, bool] = {}

    def broadcast_event(self, session_id: str, event: dict) -> None:
        self.events.append(event)

    @staticmethod
    def get_monitor_handler(session_id: str):
        return None

    @staticmethod
    def clear_pending_runtime(session_id: str) -> None:
        pass

    @staticmethod
    def clear_active_runtime(session_id: str) -> None:
        pass

    @staticmethod
    def resolve_team_agent(session_id: str):
        return None

    @staticmethod
    def pop_stream_task(session_id: str):
        return None

    @staticmethod
    def has_stream_task(session_id: str) -> bool:
        # 直接驱动 _consume_stream_with_query 时流任务视为在册（与生产一致：
        # finally 阶段的 settle 补发以此为准）
        return True

    @staticmethod
    def has_waiters(session_id: str) -> bool:
        return False

    @staticmethod
    def get_waiters(session_id: str) -> list:
        return []

    def mark_seen_team_events(self, session_id: str) -> None:
        self._seen[session_id] = True

    def has_seen_team_events(self, session_id: str) -> bool:
        return self._seen.get(session_id, False)

    def reset_seen_team_events(self, session_id: str) -> None:
        self._seen.pop(session_id, None)

    @staticmethod
    def is_workflow_completed(session_id: str) -> bool:
        return False

    @staticmethod
    def reset_workflow_completed(session_id: str) -> None:
        pass

    def mark_stream_round_terminal(self, session_id: str) -> None:
        self._terminal[session_id] = True

    def clear_stream_round_terminal(self, session_id: str) -> None:
        self._terminal.pop(session_id, None)

    def is_stream_round_terminal(self, session_id: str) -> bool:
        return self._terminal.get(session_id, False) is True


async def _run_final_round(monkeypatch: pytest.MonkeyPatch, settled: bool) -> list[dict]:
    """跑一轮「成员状态帧 + leader 正文」的迷你流，返回广播事件序列。"""
    manager = _SettleRecordingManager()
    monkeypatch.setattr(team_helpers, "get_team_manager", lambda _cid: manager)

    async def _settled(_cid, _sid):
        return settled

    monkeypatch.setattr(team_helpers, "_team_round_settled", _settled)

    def _fake_parse(chunk):
        ctype = getattr(chunk, "type", None)
        if ctype == "team.member":
            return {
                "event_type": "team.member",
                "event": {"type": "team.member.status_changed", "member_id": "m1"},
            }
        if ctype == "answer":
            return {"event_type": "chat.final", "content": chunk.payload}
        return None  # task_completion 被 parse 丢弃（生产行为）

    monkeypatch.setattr(team_helpers, "parse_stream_chunk", _fake_parse)

    async def _fake_stream(**kwargs):
        # 回合开始的成员状态帧（置位 seen_team_events）
        yield SimpleNamespace(type="team.member", payload={}, role=TeamRole.LEADER)
        yield SimpleNamespace(
            type="controller_output",
            payload=SimpleNamespace(
                type="task_completion",
                data=[SimpleNamespace(data={"output": "正文", "result_type": "answer"})],
            ),
            role=None,
        )

    monkeypatch.setattr(team_helpers.Runner, "run_agent_team_streaming", _fake_stream)
    await team_helpers._consume_stream_with_query(
        "web", "sess-settle", SimpleNamespace(team_name="spec-team"), "问", round_id=1,
    )
    return manager.events


@pytest.mark.asyncio
async def test_final_emits_terminal_when_round_settled(monkeypatch) -> None:
    """零任务问答轮：seen_team_events=True 但 settle 三件套全过 → 补发终态。"""
    events = await _run_final_round(monkeypatch, settled=True)
    finals = [e for e in events if e.get("event_type") == "chat.final"]
    assert len(finals) == 1  # 正文照常广播
    terminals = [
        e
        for e in events
        if e.get("event_type") == "chat.processing_status" and e.get("is_complete") is True
    ]
    assert len(terminals) == 1
    assert terminals[0]["is_processing"] is False


@pytest.mark.asyncio
async def test_final_forced_terminal_with_error_when_not_settled_at_stream_end(monkeypatch) -> None:
    """团队未落定（成员在途/有未读）→ 轮内不补终态（维持等 team.completed）；
    但流末仍零终态时 finally 强制补带 error 的终态帧（design/team/21 规则 2：
    状态不明不粉饰，防前端永挂）。"""
    events = await _run_final_round(monkeypatch, settled=False)
    finals = [e for e in events if e.get("event_type") == "chat.final"]
    assert len(finals) == 1
    terminals = [
        e
        for e in events
        if e.get("event_type") == "chat.processing_status" and e.get("is_complete") is True
    ]
    assert len(terminals) == 1
    assert terminals[0].get("error")


async def _run_scripted_round(
        monkeypatch: pytest.MonkeyPatch,
        settle_state: dict,
        script: str,
) -> list[dict]:
    """跑一轮可编排流：script=idle（final 后流静默一段）/ churn（final 后又有活动）。"""
    manager = _SettleRecordingManager()
    monkeypatch.setattr(team_helpers, "get_team_manager", lambda _cid: manager)
    monkeypatch.setattr(team_helpers, "_SETTLE_TERMINAL_QUIET_SEC", 0.01)

    async def _settled(_cid, _sid):
        return settle_state["v"]

    monkeypatch.setattr(team_helpers, "_team_round_settled", _settled)

    def _fake_parse(chunk):
        ctype = getattr(chunk, "type", None)
        if ctype == "team.member":
            return {
                "event_type": "team.member",
                "event": {"type": "team.member.status_changed", "member_id": "m1"},
            }
        if ctype == "answer":
            return {"event_type": "chat.final", "content": chunk.payload}
        if ctype == "tool":
            return {
                "event_type": "chat.tool_call",
                "tool_call": {"name": "bash", "tool_call_id": "c1"},
            }
        return None

    monkeypatch.setattr(team_helpers, "parse_stream_chunk", _fake_parse)

    async def _fake_stream(**kwargs):
        yield SimpleNamespace(type="team.member", payload={}, role=TeamRole.LEADER)
        yield SimpleNamespace(
            type="controller_output",
            payload=SimpleNamespace(
                type="task_completion",
                data=[SimpleNamespace(data={"output": "好的，我来拆解任务", "result_type": "answer"})],
            ),
            role=None,
        )
        if script == "churn":
            # 长流协作：leader 说完继续干活（窗口内有新流帧）
            yield SimpleNamespace(type="tool", payload={}, role=None)
            settle_state["v"] = False  # 复核时三件套已不成立
        else:
            # 静默 idle：窗口静默过去，终态应到点发出
            await asyncio.sleep(0.05)

    monkeypatch.setattr(team_helpers.Runner, "run_agent_team_streaming", _fake_stream)
    await team_helpers._consume_stream_with_query(
        "web", "sess-quiet", SimpleNamespace(team_name="spec-team"), "问", round_id=1,
    )
    return manager.events


@pytest.mark.asyncio
async def test_settle_terminal_delayed_and_fired_when_idle(monkeypatch) -> None:
    """静默窗内无新活动：终态延迟到点照发（流还开着也能收到）。"""
    events = await _run_scripted_round(monkeypatch, {"v": True}, script="idle")
    terminals = [
        e
        for e in events
        if e.get("event_type") == "chat.processing_status" and e.get("is_complete") is True
    ]
    assert len(terminals) == 1


@pytest.mark.asyncio
async def test_settle_terminal_cancelled_by_new_activity(monkeypatch) -> None:
    """长流协作防误杀：final 后窗口内又有流帧（leader 说完继续干活）→ 静默窗
    终态作废（轮内不误收）；流末仍零终态 → finally 强制补带 error 终态
    （design/team/21 规则 2：流尽=本流视角回合已了，复核 False 不粉饰）。"""
    events = await _run_scripted_round(monkeypatch, {"v": True}, script="churn")
    terminals = [
        e
        for e in events
        if e.get("event_type") == "chat.processing_status" and e.get("is_complete") is True
    ]
    assert len(terminals) == 1
    assert terminals[0].get("error")


async def _run_retry_round(monkeypatch: pytest.MonkeyPatch, settle_seq: list, trailing: str) -> list[dict]:
    """settle 读数按序列返回（None=未就绪），final 后带尾随帧。trailing=usage/content。"""
    manager = _SettleRecordingManager()
    monkeypatch.setattr(team_helpers, "get_team_manager", lambda _cid: manager)
    monkeypatch.setattr(team_helpers, "_SETTLE_TERMINAL_QUIET_SEC", 0.01)
    state = {"calls": 0}

    async def _settled(_cid, _sid):
        idx = min(state["calls"], len(settle_seq) - 1)
        state["calls"] += 1
        return settle_seq[idx]

    monkeypatch.setattr(team_helpers, "_team_round_settled", _settled)

    def _fake_parse(chunk):
        ctype = getattr(chunk, "type", None)
        if ctype == "team.member":
            return {
                "event_type": "team.member",
                "event": {"type": "team.member.status_changed", "member_id": "m1"},
            }
        if ctype == "answer":
            return {"event_type": "chat.final", "content": chunk.payload}
        if ctype == "usage":
            return {"event_type": "chat.usage_metadata"}
        if ctype == "tool":
            return {"event_type": "chat.tool_call", "tool_call": {"name": "bash", "tool_call_id": "c1"}}
        return None

    monkeypatch.setattr(team_helpers, "parse_stream_chunk", _fake_parse)

    async def _fake_stream(**kwargs):
        yield SimpleNamespace(type="team.member", payload={}, role=TeamRole.LEADER)
        yield SimpleNamespace(
            type="controller_output",
            payload=SimpleNamespace(
                type="task_completion",
                data=[SimpleNamespace(data={"output": "正文", "result_type": "answer"})],
            ),
            role=None,
        )
        if trailing == "usage":
            yield SimpleNamespace(type="usage", payload={}, role=None)
        elif trailing == "content":
            yield SimpleNamespace(type="tool", payload={}, role=None)
        await asyncio.sleep(0.08)

    monkeypatch.setattr(team_helpers.Runner, "run_agent_team_streaming", _fake_stream)
    await team_helpers._consume_stream_with_query(
        "web", "sess-retry", SimpleNamespace(team_name="spec-team"), "问", round_id=1,
    )
    return manager.events


@pytest.mark.asyncio
async def test_settle_terminal_retries_when_readings_not_ready(monkeypatch) -> None:
    """读数未就绪（None）续窗重试：两次 None 后变 True → 终态补发（第三种场景）。"""
    events = await _run_retry_round(monkeypatch, [None, None, True], trailing="usage")
    terminals = [
        e for e in events
        if e.get("event_type") == "chat.processing_status" and e.get("is_complete") is True
    ]
    assert len(terminals) == 1


@pytest.mark.asyncio
async def test_usage_trailing_frame_does_not_cancel_window(monkeypatch) -> None:
    """final 后的 usage 尾随帧不算新活动：静默窗不被误作废，终态照发。"""
    events = await _run_retry_round(monkeypatch, [True], trailing="usage")
    terminals = [
        e for e in events
        if e.get("event_type") == "chat.processing_status" and e.get("is_complete") is True
    ]
    assert len(terminals) == 1


@pytest.mark.asyncio
async def test_unknown_forever_abandons_after_max_attempts(monkeypatch) -> None:
    """读数持续未就绪超过重试上限 → 静默窗放弃；流末零终态 → finally 强制补
    带 error 终态（design/team/21 规则 2：放弃重试≠放弃收尾）。"""
    events = await _run_retry_round(monkeypatch, [None, None, None], trailing="usage")
    terminals = [
        e for e in events
        if e.get("event_type") == "chat.processing_status" and e.get("is_complete") is True
    ]
    assert len(terminals) == 1
    assert terminals[0].get("error")


# ------------------------------------------------------------------
# design/team/21：终态族治理（问题 3 重复去重 + 问题 2 流末强制补终态）
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_duplicate_terminal_when_team_completed_follows_final(monkeypatch) -> None:
    """问题 3 回归钉：final 挂起 settle 静默窗 → team.completed 转换已发终态 →
    流末 finally 兜底不得重复补发（_emit_settle_terminal 入口查 completion_signals）。
    修复前此场景必现 2 条终态帧（第二条无计数）。"""
    manager = _SettleRecordingManager()
    monkeypatch.setattr(team_helpers, "get_team_manager", lambda _cid: manager)

    async def _settled(_cid, _sid):
        return True

    monkeypatch.setattr(team_helpers, "_team_round_settled", _settled)

    def _fake_parse(chunk):
        ctype = getattr(chunk, "type", None)
        if ctype == "team.member":
            return {
                "event_type": "team.member",
                "event": {"type": "team.member.status_changed", "member_id": "m1"},
            }
        if ctype == "answer":
            return {"event_type": "chat.final", "content": chunk.payload}
        if ctype == "completed":
            return {"event_type": "team.completed", "member_count": 3, "task_count": 2}
        return None

    monkeypatch.setattr(team_helpers, "parse_stream_chunk", _fake_parse)

    async def _fake_stream(**kwargs):
        yield SimpleNamespace(type="team.member", payload={}, role=TeamRole.LEADER)
        yield SimpleNamespace(
            type="controller_output",
            payload=SimpleNamespace(
                type="task_completion",
                data=[SimpleNamespace(data={"output": "总结", "result_type": "answer"})],
            ),
            role=None,
        )
        yield SimpleNamespace(type="completed", payload={}, role=None)

    monkeypatch.setattr(team_helpers.Runner, "run_agent_team_streaming", _fake_stream)
    await team_helpers._consume_stream_with_query(
        "web", "sess-dup", SimpleNamespace(team_name="spec-team"), "问", round_id=1,
    )
    terminals = [
        e for e in manager.events
        if e.get("event_type") == "chat.processing_status" and e.get("is_complete") is True
    ]
    assert len(terminals) == 1
    # 唯一终态 = team.completed 转换帧（带计数），settle/finally 均未重复补发
    assert terminals[0].get("member_count") == 3
    assert terminals[0].get("task_count") == 2


@pytest.mark.asyncio
async def test_forced_clean_terminal_when_stream_ends_without_any_terminal(monkeypatch) -> None:
    """问题 2：流结束仍零终态（无 final、无 team.completed，settle 从未挂窗）
    且复核落定 → finally 强制补干净终态帧（不带 error）。"""
    manager = _SettleRecordingManager()
    monkeypatch.setattr(team_helpers, "get_team_manager", lambda _cid: manager)

    async def _settled(_cid, _sid):
        return True

    monkeypatch.setattr(team_helpers, "_team_round_settled", _settled)

    def _fake_parse(chunk):
        if getattr(chunk, "type", None) == "team.member":
            return {
                "event_type": "team.member",
                "event": {"type": "team.member.status_changed", "member_id": "m1"},
            }
        return None

    monkeypatch.setattr(team_helpers, "parse_stream_chunk", _fake_parse)

    async def _fake_stream(**kwargs):
        yield SimpleNamespace(type="team.member", payload={}, role=TeamRole.LEADER)

    monkeypatch.setattr(team_helpers.Runner, "run_agent_team_streaming", _fake_stream)
    await team_helpers._consume_stream_with_query(
        "web", "sess-force", SimpleNamespace(team_name="spec-team"), "问", round_id=1,
    )
    terminals = [
        e for e in manager.events
        if e.get("event_type") == "chat.processing_status" and e.get("is_complete") is True
    ]
    assert len(terminals) == 1
    assert "error" not in terminals[0]


@pytest.mark.asyncio
async def test_no_forced_terminal_when_interaction_pending(monkeypatch) -> None:
    """挂起交互守卫：leader 发出 ask_user_question 后流结束（回合挂起等用户输入）→
    不强制补终态（暂停待恢复，不是终结）。"""
    manager = _SettleRecordingManager()
    monkeypatch.setattr(team_helpers, "get_team_manager", lambda _cid: manager)

    def _fake_parse(chunk):
        ctype = getattr(chunk, "type", None)
        if ctype == "team.member":
            return {
                "event_type": "team.member",
                "event": {"type": "team.member.status_changed", "member_id": "m1"},
            }
        if ctype == "ask":
            return {
                "event_type": "chat.ask_user_question",
                "request_id": "ask-1",
                "questions": [{"question": "确认继续吗？"}],
            }
        return None

    monkeypatch.setattr(team_helpers, "parse_stream_chunk", _fake_parse)

    async def _fake_stream(**kwargs):
        yield SimpleNamespace(type="team.member", payload={}, role=TeamRole.LEADER)
        yield SimpleNamespace(type="ask", payload={}, role=TeamRole.LEADER)

    monkeypatch.setattr(team_helpers.Runner, "run_agent_team_streaming", _fake_stream)
    await team_helpers._consume_stream_with_query(
        "web", "sess-hitl", SimpleNamespace(team_name="spec-team"), "问", round_id=1,
    )
    terminals = [
        e for e in manager.events
        if e.get("event_type") == "chat.processing_status" and e.get("is_complete") is True
    ]
    assert terminals == []


class _PauseInFlightManager(_SettleRecordingManager):
    """pause 进行中的 TeamManager fake。"""

    def is_pause_in_progress(self, session_id: str) -> bool:
        return True


@pytest.mark.asyncio
async def test_no_forced_terminal_when_pause_in_progress(monkeypatch) -> None:
    """pause 守卫：用户主动停止（intent=pause）/判死级联 park 导致的
    流结束 → 不强制补终态（回合挂起待续而非终结；settle 复核在 pause 执行中
    必然非 True，补了必是带 error 的误伤帧，前端「已停止」被翻成「失败」）。"""
    manager = _PauseInFlightManager()
    monkeypatch.setattr(team_helpers, "get_team_manager", lambda _cid: manager)

    forced_calls: list = []
    real_emit = team_helpers._emit_forced_terminal_at_stream_end

    async def _spy_emit(*args, **kwargs):
        forced_calls.append((args, kwargs))
        await real_emit(*args, **kwargs)

    monkeypatch.setattr(
        team_helpers, "_emit_forced_terminal_at_stream_end", _spy_emit
    )

    def _fake_parse(chunk):
        if getattr(chunk, "type", None) == "team.member":
            return {
                "event_type": "team.member",
                "event": {"type": "team.member.status_changed", "member_id": "m1"},
            }
        return None

    monkeypatch.setattr(team_helpers, "parse_stream_chunk", _fake_parse)

    async def _fake_stream(**kwargs):
        yield SimpleNamespace(type="team.member", payload={}, role=TeamRole.LEADER)

    monkeypatch.setattr(team_helpers.Runner, "run_agent_team_streaming", _fake_stream)
    await team_helpers._consume_stream_with_query(
        "web", "sess-pause", SimpleNamespace(team_name="spec-team"), "问", round_id=1,
    )
    # 强制补终态未被调用（pause 守卫在 settle 复核前短路）
    assert forced_calls == []
    terminals = [
        e for e in manager.events
        if e.get("event_type") == "chat.processing_status" and e.get("is_complete") is True
    ]
    assert terminals == []
    # 回合挂起待续：不得标记轮次终态（下一条消息按 RESUME_FROM_PAUSE 新流处理）
    assert manager.is_stream_round_terminal("sess-pause") is False
    # team.completed 收尾广播不受影响（cron watcher 收尾依赖）
    assert any(e.get("event_type") == "team.completed" for e in manager.events)


@pytest.mark.asyncio
async def test_no_forced_terminal_when_stream_cancelled(monkeypatch) -> None:
    """取消守卫：流被取消（用户停止/级联收流）→ 不强制补终态。"""
    manager = _SettleRecordingManager()
    monkeypatch.setattr(team_helpers, "get_team_manager", lambda _cid: manager)

    def _fake_parse(chunk):
        if getattr(chunk, "type", None) == "team.member":
            return {
                "event_type": "team.member",
                "event": {"type": "team.member.status_changed", "member_id": "m1"},
            }
        return None

    monkeypatch.setattr(team_helpers, "parse_stream_chunk", _fake_parse)

    async def _fake_stream(**kwargs):
        yield SimpleNamespace(type="team.member", payload={}, role=TeamRole.LEADER)
        raise asyncio.CancelledError()

    monkeypatch.setattr(team_helpers.Runner, "run_agent_team_streaming", _fake_stream)
    with pytest.raises(asyncio.CancelledError):
        await team_helpers._consume_stream_with_query(
            "web", "sess-cancel", SimpleNamespace(team_name="spec-team"), "问", round_id=1,
        )
    terminals = [
        e for e in manager.events
        if e.get("event_type") == "chat.processing_status" and e.get("is_complete") is True
    ]
    assert terminals == []


@pytest.mark.asyncio
async def test_forced_terminal_failure_does_not_block_team_completed(monkeypatch) -> None:
    """finally 内补终态自身失败（广播异常）→ 仅记日志不传播：不遮蔽原异常、
    不跳过后续 raw team.completed 广播（cron watcher 收尾依赖）。"""
    manager = _SettleRecordingManager()
    monkeypatch.setattr(team_helpers, "get_team_manager", lambda _cid: manager)

    async def _settled(_cid, _sid):
        return True

    monkeypatch.setattr(team_helpers, "_team_round_settled", _settled)

    def _fake_parse(chunk):
        if getattr(chunk, "type", None) == "team.member":
            return {
                "event_type": "team.member",
                "event": {"type": "team.member.status_changed", "member_id": "m1"},
            }
        return None

    monkeypatch.setattr(team_helpers, "parse_stream_chunk", _fake_parse)

    async def _fake_stream(**kwargs):
        yield SimpleNamespace(type="team.member", payload={}, role=TeamRole.LEADER)

    monkeypatch.setattr(team_helpers.Runner, "run_agent_team_streaming", _fake_stream)

    real_broadcast = team_helpers._broadcast_event

    async def _flaky_broadcast(cid, sid, event):
        # 只在强制终态帧上炸（终态帧特征：processing_status 且 is_complete）
        if event.get("event_type") == "chat.processing_status" and event.get("is_complete") is True:
            raise RuntimeError("waiter queue exploded")
        await real_broadcast(cid, sid, event)

    monkeypatch.setattr(team_helpers, "_broadcast_event", _flaky_broadcast)

    await team_helpers._consume_stream_with_query(
        "web", "sess-flaky", SimpleNamespace(team_name="spec-team"), "问", round_id=1,
    )
    # 补终态失败后：raw team.completed 兜底广播仍须到达
    assert any(e.get("event_type") == "team.completed" for e in manager.events)
