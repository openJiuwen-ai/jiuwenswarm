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
        self._degraded_sessions: dict[str, str] = {}
        self._user_turn_epochs: dict[str, int] = {}

    def bump_user_turn_epoch(self, session_id: str) -> int:
        epoch = self._user_turn_epochs.get(session_id, 0) + 1
        self._user_turn_epochs[session_id] = epoch
        return epoch

    def user_turn_epoch(self, session_id: str) -> int:
        return self._user_turn_epochs.get(session_id, 0)

    def mark_session_degraded(self, session_id: str, detail: str = "") -> None:
        self._degraded_sessions[session_id] = detail

    def has_session_degraded(self, session_id: str) -> bool:
        return session_id in self._degraded_sessions

    def session_degraded_detail(self, session_id: str) -> str | None:
        return self._degraded_sessions.get(session_id)

    def clear_session_degraded(self, session_id: str) -> None:
        self._degraded_sessions.pop(session_id, None)

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


from jiuwenswarm.agents.harness.team.handlers.team_monitor_handler import (
    TeamNotEstablishedError,
)


@pytest.mark.asyncio
async def test_not_established_propagates_from_db_snapshot(monkeypatch) -> None:
    """live 快照不可得 + DB 对照抛 TeamNotEstablishedError → 原样上抛（不归 None）。

    None 会让调用方续窗重试，而"团队从未建立"
    等到进程退出也不会好，续窗=永不收口。
    """
    _patch_progress(monkeypatch, None)

    async def _db_raise(_cid, _sid):
        raise TeamNotEstablishedError("team_info has no row for team 't'")

    monkeypatch.setattr(team_stall_watchdog, "team_progress_snapshot_db", _db_raise)
    with pytest.raises(TeamNotEstablishedError):
        await team_helpers._team_round_settled("desktop", "s1")


@pytest.mark.asyncio
async def test_not_established_propagates_from_unread_check(monkeypatch) -> None:
    """快照正常但动态消息表不存在（unread 检查抛出）→ 同样原样上抛。"""
    _patch_progress(monkeypatch, (0, 0, False))

    async def _unread_raise(_cid, _sid):
        raise TeamNotEstablishedError("no such table: team_message_x")

    monkeypatch.setattr(team_helpers, "_team_has_unread_messages", _unread_raise)
    with pytest.raises(TeamNotEstablishedError):
        await team_helpers._team_round_settled("desktop", "s1")


@pytest.mark.asyncio
async def test_transient_db_failure_still_returns_none(monkeypatch) -> None:
    """瞬态读失败（database is locked 等普通异常）→ 维持 None 续窗语义。"""
    _patch_progress(monkeypatch, None)

    async def _db_locked(_cid, _sid):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(team_stall_watchdog, "team_progress_snapshot_db", _db_locked)
    assert await team_helpers._team_round_settled("desktop", "s1") is None


@pytest.mark.asyncio
async def test_snapshot_db_reraises_not_established_but_swallows_transient(monkeypatch) -> None:
    """team_progress_snapshot_db：TeamNotEstablishedError 穿透，其余异常归 None。"""
    from jiuwenswarm.agents.harness.team.handlers import team_monitor_handler

    class _Mgr:
        @staticmethod
        def get_active_team_name(session_id):
            return "demo-team"

    monkeypatch.setattr(team_stall_watchdog, "get_team_manager", lambda _cid: _Mgr())

    async def _raise_not_established(_sid, _tname):
        raise TeamNotEstablishedError("never built")

    monkeypatch.setattr(
        team_monitor_handler.TeamMonitorHandler,
        "get_team_snapshot_from_db",
        staticmethod(_raise_not_established),
    )
    with pytest.raises(TeamNotEstablishedError):
        await team_stall_watchdog.team_progress_snapshot_db("desktop", "s1")

    async def _raise_locked(_sid, _tname):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(
        team_monitor_handler.TeamMonitorHandler,
        "get_team_snapshot_from_db",
        staticmethod(_raise_locked),
    )
    assert await team_stall_watchdog.team_progress_snapshot_db("desktop", "s1") is None


@pytest.mark.asyncio
async def test_final_emits_error_terminal_and_cascades_when_team_not_established(monkeypatch) -> None:
    """集成：leader final 后 settle 复核撞 TeamNotEstablishedError →
    经静默窗（等降级告警帧的赛跑窗）补带 error 的 PS 终态 + 级联 park 收流。"""
    manager = _SettleRecordingManager()
    monkeypatch.setattr(team_helpers, "get_team_manager", lambda _cid: manager)
    # 压缩静默窗（终审修订：不再立即判死，给降级告警帧一个赛跑窗）
    monkeypatch.setattr(team_helpers, "_SETTLE_TERMINAL_QUIET_SEC", 0.01)

    cascaded: list[dict] = []

    def _fake_cascade(channel_id, session_id, round_id, *, source):
        cascaded.append({"session_id": session_id, "source": source})

    monkeypatch.setattr(
        team_helpers, "_cascade_park_runtime_after_terminal", _fake_cascade
    )

    async def _raise_not_established(_cid, _sid):
        raise TeamNotEstablishedError("team_info has no row for team 'spec-team'")

    monkeypatch.setattr(team_helpers, "_team_round_settled", _raise_not_established)

    def _fake_parse(chunk):
        ctype = getattr(chunk, "type", None)
        if ctype == "tool":
            # 建团尝试过（build_team）但失败——判死分支的
            # 到达前提是本轮调过 build_team（纯单干轮不判死）。
            # 刻意用顶层 tool_name 形态（检视第十轮：提取口径须覆盖顶层
            # tool_name 与嵌套 tool_call.name 两种——本例钉顶层，
            # 嵌套形态由 test_team_helpers 的 solo 测试反向覆盖）
            return {
                "event_type": "chat.tool_call",
                "tool_name": "build_team",
                "tool_call": {"tool_call_id": "c1"},
            }
        if ctype == "answer":
            return {"event_type": "chat.final", "content": chunk.payload}
        return None

    monkeypatch.setattr(team_helpers, "parse_stream_chunk", _fake_parse)

    async def _fake_stream(**kwargs):
        # 真实「从未建立」形态：无团队事件帧（成员从未存在，否则按主动解散
        # 干净收口）——只有 build_team 类工具调用（非干净纯文本
        # 轮）+ leader final
        yield SimpleNamespace(type="tool", payload={}, role=TeamRole.LEADER)
        yield SimpleNamespace(type="answer", payload="调查结论：建团失败", role=TeamRole.LEADER)
        # 流保持足够久让静默窗到点
        await asyncio.sleep(0.05)

    monkeypatch.setattr(team_helpers.Runner, "run_agent_team_streaming", _fake_stream)
    await team_helpers._consume_stream_with_query(
        "web", "sess-never-built", SimpleNamespace(team_name="spec-team"), "问", round_id=1,
    )

    terminals = [
        e
        for e in manager.events
        if e.get("event_type") == "chat.processing_status" and e.get("is_complete") is True
    ]
    assert len(terminals) == 1
    assert terminals[0]["is_processing"] is False
    assert "never built" in (terminals[0].get("error") or "") or "no row" in (
            terminals[0].get("error") or ""
    )
    assert cascaded and cascaded[0]["source"] == "settle-team-not-established"


@pytest.mark.asyncio
async def test_forced_terminal_at_stream_end_carries_not_established_reason(monkeypatch) -> None:
    """流末强制补终态撞上团队从未建立：异常不得逃出（finally 路径），
    且 error 带具体原因而非通用不明文案。"""

    async def _raise(_cid, _sid):
        raise TeamNotEstablishedError("team_info has no row for team 't'")

    monkeypatch.setattr(team_helpers, "_team_round_settled", _raise)
    broadcasted: list[dict] = []

    async def _rec(_cid, _sid, event):
        broadcasted.append(event)

    monkeypatch.setattr(team_helpers, "_broadcast_event", _rec)

    await team_helpers._emit_forced_terminal_at_stream_end(
        "web", "s1", 1, unrecovered_error=None
    )
    assert len(broadcasted) == 1
    terminal = broadcasted[0]
    assert terminal["is_complete"] is True
    assert terminal["is_processing"] is False
    assert "no row" in terminal["error"]


@pytest.mark.asyncio
async def test_forced_terminal_degraded_zero_content_carries_error(monkeypatch) -> None:
    """流末兜底 + 降级会话 + 零产出：不得补干净终态（空回合记已完成）——
    error 复用降级告警详情（检视意见第四轮）。"""
    from jiuwenswarm.agents.harness.team.handlers.team_monitor_handler import (
        TeamNotEstablishedError,
    )

    async def _raise(_cid, _sid):
        raise TeamNotEstablishedError("team_info has no row for team 't'")

    monkeypatch.setattr(team_helpers, "_team_round_settled", _raise)
    manager = _SettleRecordingManager()
    manager.mark_session_degraded("s1", "[132001] build_team failed: db locked")
    monkeypatch.setattr(team_helpers, "get_team_manager", lambda _cid: manager)
    broadcasted: list[dict] = []

    async def _rec(_cid, _sid, event):
        broadcasted.append(event)

    monkeypatch.setattr(team_helpers, "_broadcast_event", _rec)

    await team_helpers._emit_forced_terminal_at_stream_end(
        "web", "s1", 1, unrecovered_error=None, round_produced_content=False
    )
    assert len(broadcasted) == 1
    terminal = broadcasted[0]
    assert terminal["is_complete"] is True
    assert "132001" in terminal["error"]

    # 对照：降级会话但有实质产出 → 干净终态（不误伤正常降级轮）
    broadcasted.clear()
    await team_helpers._emit_forced_terminal_at_stream_end(
        "web", "s1", 1, unrecovered_error=None, round_produced_content=True
    )
    assert len(broadcasted) == 1
    assert "error" not in broadcasted[0]


@pytest.mark.asyncio
async def test_unread_check_reraises_not_established_through_real_impl(monkeypatch) -> None:
    """直接驱动真实 _team_has_unread_messages：no such table 必须穿透外层
    except Exception（regression：内层 raise 经 finally 后会被外层吞回 None）。"""
    from jiuwenswarm.agents.harness.team import config_loader
    from jiuwenswarm.common import config as common_config
    from openjiuwen.agent_teams.spawn import shared_resources

    class _Mgr:
        @staticmethod
        def get_active_team_name(session_id):
            return "demo-team"

    monkeypatch.setattr(team_helpers, "get_team_manager", lambda _cid: _Mgr())
    monkeypatch.setattr(
        config_loader, "resolve_team_sqlite_db_path", lambda _cfg: "/tmp/team.db"
    )
    monkeypatch.setattr(common_config, "get_config", lambda: object())

    async def _noop(*_a, **_kw):
        return None

    async def _no_such_table(_team_name):
        raise RuntimeError("(sqlite3.OperationalError) no such table: team_message_x")

    fake_db = SimpleNamespace(
        initialize=_noop,
        create_cur_session_tables=_noop,
        message=SimpleNamespace(has_unread_messages=_no_such_table),
    )
    monkeypatch.setattr(shared_resources, "get_shared_db", lambda _cfg: fake_db)

    with pytest.raises(TeamNotEstablishedError):
        await team_helpers._team_has_unread_messages("desktop", "s1")


@pytest.mark.asyncio
async def test_unread_check_transient_error_still_none(monkeypatch) -> None:
    """同一通路撞瞬态错误（database is locked）→ 仍归 None。"""
    from jiuwenswarm.agents.harness.team import config_loader
    from jiuwenswarm.common import config as common_config
    from openjiuwen.agent_teams.spawn import shared_resources

    class _Mgr:
        @staticmethod
        def get_active_team_name(session_id):
            return "demo-team"

    monkeypatch.setattr(team_helpers, "get_team_manager", lambda _cid: _Mgr())
    monkeypatch.setattr(
        config_loader, "resolve_team_sqlite_db_path", lambda _cfg: "/tmp/team.db"
    )
    monkeypatch.setattr(common_config, "get_config", lambda: object())

    async def _noop(*_a, **_kw):
        return None

    async def _locked(_team_name):
        raise RuntimeError("(sqlite3.OperationalError) database is locked")

    fake_db = SimpleNamespace(
        initialize=_noop,
        create_cur_session_tables=_noop,
        message=SimpleNamespace(has_unread_messages=_locked),
    )
    monkeypatch.setattr(shared_resources, "get_shared_db", lambda _cfg: fake_db)

    assert await team_helpers._team_has_unread_messages("desktop", "s1") is None
