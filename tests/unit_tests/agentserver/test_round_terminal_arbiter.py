"""RoundTerminalArbiter 单测
"""

from __future__ import annotations

import asyncio

import pytest

from jiuwenswarm.agents.harness.team.handlers.team_monitor_handler import (
    TeamNotEstablishedError,
)
from jiuwenswarm.server.runtime.agent_adapter import round_terminal_arbiter as arb_mod
from jiuwenswarm.server.runtime.agent_adapter.round_terminal_arbiter import (
    Outcome,
    ProposalSource,
    RoundTerminalArbiter,
)


def _fast_windows(monkeypatch: pytest.MonkeyPatch, *, quiet: float = 0.01) -> None:
    monkeypatch.setattr(arb_mod, "SETTLE_QUIET_SEC", quiet)
    monkeypatch.setattr(arb_mod, "SETTLE_FALSE_RECHECK_SEC", 0.01)
    monkeypatch.setattr(arb_mod, "SETTLE_FALSE_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(arb_mod, "SETTLE_RECHECK_DEBOUNCE_SEC", 0)


class _Harness:
    """最小外部世界 fake：广播记录 + 级联记录 + 打标记录 + 可编排 settle 读数。"""

    def __init__(
            self,
            settled_seq=None,
            *,
            degraded: bool = False,
            degraded_detail: str | None = None,
            pause: bool = False,
            stream_alive: bool = True,
    ) -> None:
        self.events: list[dict] = []
        self.cascades: list[str] = []
        self.terminal_marked = False
        self.settled_calls = 0
        self._seq = list(settled_seq) if settled_seq is not None else [True]
        self._degraded = degraded
        self._degraded_detail = degraded_detail
        self._pause = pause
        self._stream_alive = stream_alive

    def build(self) -> RoundTerminalArbiter:
        async def _broadcast(payload: dict) -> None:
            self.events.append(payload)

        async def _settled_check():
            idx = min(self.settled_calls, len(self._seq) - 1)
            self.settled_calls += 1
            value = self._seq[idx]
            if isinstance(value, Exception):
                raise value
            return value

        def _mark() -> None:
            self.terminal_marked = True

        def _clear() -> None:
            self.terminal_marked = False

        return RoundTerminalArbiter(
            session_id="s1",
            round_id=1,
            broadcast=_broadcast,
            settled_check=_settled_check,
            has_stream_task=lambda: self._stream_alive,
            is_pause_in_progress=lambda: self._pause,
            mark_round_terminal=_mark,
            clear_round_terminal=_clear,
            cascade=lambda src: self.cascades.append(src),
            has_session_degraded=lambda: self._degraded,
            session_degraded_detail=lambda: self._degraded_detail,
        )

    def terminals(self) -> list[dict]:
        return [
            e
            for e in self.events
            if e.get("event_type") == "chat.processing_status"
               and e.get("is_complete") is True
        ]


@pytest.mark.asyncio
async def test_team_completed_clean_with_stats() -> None:
    h = _Harness()
    arb = h.build()
    outcome = await arb.propose(
        {"source": ProposalSource.TEAM_COMPLETED, "member_count": 3, "task_count": 5}
    )
    assert outcome == Outcome.EMITTED_CLEAN
    terminals = h.terminals()
    assert len(terminals) == 1
    assert terminals[0]["member_count"] == 3
    assert terminals[0]["task_count"] == 5
    assert "error" not in terminals[0]
    assert h.terminal_marked is True


@pytest.mark.asyncio
async def test_error_history_attached_on_any_terminal() -> None:
    """错误史仍挂着 → 任意路径终态必带 error（含 team_completed 与
    instant_final——现行 instant 路径不携带，裁决器按设计补齐）。"""
    h = _Harness()
    arb = h.build()
    arb.note_leader_error("model call failed 181001")
    outcome = await arb.propose({"source": ProposalSource.TEAM_COMPLETED})
    assert outcome == Outcome.EMITTED_ERROR
    assert h.terminals()[0]["error"] == "model call failed 181001"


@pytest.mark.asyncio
async def test_dedup_suppresses_all_sources_after_terminal() -> None:
    """终态后全部提案源一律否决（含现行无去重的 team_completed——
    裁决器统一前置闸）。"""
    h = _Harness()
    arb = h.build()
    assert await arb.propose({"source": ProposalSource.INSTANT_FINAL}) == Outcome.EMITTED_CLEAN
    for source in (
            ProposalSource.TEAM_COMPLETED,
            ProposalSource.INSTANT_FINAL,
            ProposalSource.SETTLE,
            ProposalSource.DEATH_PROBE,
            ProposalSource.STALL_WATCHDOG,
            ProposalSource.STREAM_END,
            ProposalSource.INTERACT_FAILED,
    ):
        assert await arb.propose({"source": source}) == Outcome.SUPPRESSED, source
    assert len(h.terminals()) == 1


@pytest.mark.asyncio
async def test_rearm_only_on_leader_content() -> None:
    """再武装只认 leader 内容帧：成员尾随帧不复位去重账本。"""
    h = _Harness()
    arb = h.build()
    await arb.propose({"source": ProposalSource.INSTANT_FINAL})
    # 成员内容帧：不再武装
    arb.note_content_frame(is_leader=False)
    assert await arb.propose({"source": ProposalSource.INSTANT_FINAL}) == Outcome.SUPPRESSED
    # leader 内容帧：再武装 → 新一轮可再发终态
    arb.note_content_frame(is_leader=True)
    assert arb.completion_signals == 0
    assert h.terminal_marked is False  # 清标（反向豁免）
    assert await arb.propose({"source": ProposalSource.INSTANT_FINAL}) == Outcome.EMITTED_CLEAN
    assert len(h.terminals()) == 2


@pytest.mark.asyncio
async def test_settle_true_defers_then_clean_terminal(monkeypatch) -> None:
    _fast_windows(monkeypatch)
    h = _Harness(settled_seq=[True])
    arb = h.build()
    assert await arb.propose({"source": ProposalSource.SETTLE}) == Outcome.DEFERRED
    assert arb.quiet_window_pending is True
    await asyncio.sleep(0.05)
    terminals = h.terminals()
    assert len(terminals) == 1
    assert "error" not in terminals[0]


@pytest.mark.asyncio
async def test_settle_none_retries_then_abandons(monkeypatch) -> None:
    """读数持续未就绪超过 3 次 → 放弃（不补终态，维持等 team.completed）。"""
    _fast_windows(monkeypatch)
    h = _Harness(settled_seq=[None])
    arb = h.build()
    assert await arb.propose({"source": ProposalSource.SETTLE}) == Outcome.DEFERRED
    await asyncio.sleep(0.08)
    assert h.terminals() == []
    assert arb.completion_signals == 0


@pytest.mark.asyncio
async def test_settle_false_bounded_recheck_closes_clean(monkeypatch) -> None:
    """复现钉：final 当口 False（时序窗误拒）→ 置重评标记 + 有界
    续窗，读数转 True 即补干净终态（不再裸赌 team.completed）。"""
    _fast_windows(monkeypatch)
    h = _Harness(settled_seq=[False, True, True])
    arb = h.build()
    assert await arb.propose({"source": ProposalSource.SETTLE}) == Outcome.DEFERRED
    assert arb.recheck_pending is True
    await asyncio.sleep(0.06)
    terminals = h.terminals()
    assert len(terminals) == 1
    assert "error" not in terminals[0]
    # 终态发射点统一复位：重评标记清除（跨轮不残留）
    assert arb.recheck_pending is False


@pytest.mark.asyncio
async def test_settle_false_persistent_no_forced_terminal(monkeypatch) -> None:
    """持续 False = 确有在途：耗尽续窗不强制收口（防误杀长模型调用合法
    静默）。"""
    _fast_windows(monkeypatch)
    h = _Harness(settled_seq=[False])
    arb = h.build()
    assert await arb.propose({"source": ProposalSource.SETTLE}) == Outcome.DEFERRED
    await asyncio.sleep(0.1)  # 0.01 + 3×0.01 耗尽
    assert h.terminals() == []
    assert arb.completion_signals == 0


@pytest.mark.asyncio
async def test_event_recheck_reschedules_after_voided_window(monkeypatch) -> None:
    """路径：静默窗被新内容帧作废后，团队状态帧触发事件驱动
    重评（后台补评任务，不内联），True 且窗口已死时补调度 → 干净终态。"""
    _fast_windows(monkeypatch, quiet=0.02)
    h = _Harness(settled_seq=[False, True, True])
    arb = h.build()
    await arb.propose({"source": ProposalSource.SETTLE})
    # 窗口排队期间来新内容帧 → 作废
    arb.note_content_frame(is_leader=False)
    await asyncio.sleep(0.05)  # 作废的窗口到点返回（无终态）
    assert h.terminals() == []
    # 团队落定：状态帧调度补评 → True → 补调度 → 干净终态
    await arb.note_state_frame()
    await asyncio.sleep(0.08)
    terminals = h.terminals()
    assert len(terminals) == 1
    assert "error" not in terminals[0]


@pytest.mark.asyncio
async def test_event_recheck_debounced(monkeypatch) -> None:
    """去抖（形态）：状态帧不内联点查（调用方零阻塞），帧簇只
    产生一次补评；去抖窗过后的新帧再触发一轮。"""
    monkeypatch.setattr(arb_mod, "SETTLE_RECHECK_DEBOUNCE_SEC", 0.05)
    monkeypatch.setattr(arb_mod, "SETTLE_QUIET_SEC", 60)
    h = _Harness(settled_seq=[False, True, True])
    arb = h.build()
    await arb.propose({"source": ProposalSource.SETTLE})
    assert h.settled_calls == 1
    await arb.note_state_frame()
    await arb.note_state_frame()  # 帧簇第二帧：补评任务已在途，不重复调度
    assert h.settled_calls == 1  # 不内联点查：调用方零阻塞
    await asyncio.sleep(0.1)  # trailing 到点补评一次
    assert h.settled_calls == 2
    await arb.note_state_frame()  # 去抖窗已过：再触发一轮
    await asyncio.sleep(0.1)
    assert h.settled_calls == 3
    arb.close()


@pytest.mark.asyncio
async def test_recheck_flag_cleared_by_terminal_from_other_source(monkeypatch) -> None:
    """标记生命周期：settle False 置标后 team_completed 先到 → 终态发射
    统一复位清标记，后续状态帧不再驱动重评。"""
    _fast_windows(monkeypatch, quiet=60)
    h = _Harness(settled_seq=[False, True])
    arb = h.build()
    await arb.propose({"source": ProposalSource.SETTLE})
    assert arb.recheck_pending is True
    await arb.propose({"source": ProposalSource.TEAM_COMPLETED})
    assert arb.recheck_pending is False
    calls_before = h.settled_calls
    await arb.note_state_frame()  # 无标记：不调度补评
    await asyncio.sleep(0.02)  # 亦无延迟触发
    assert h.settled_calls == calls_before
    arb.close()


@pytest.mark.asyncio
async def test_recheck_tail_frame_not_lost(monkeypatch) -> None:
    """复现钉（裁决器同构）：定时续窗耗尽后帧簇同批到达——首帧调度的
    trailing 补评在 0.5s 窗满后执行，届时尾帧携带的落定迁移已生效，
    不丢决定性帧 → 干净终态。旧 leading-edge 形态：首帧即评 False、
    尾帧去抖跳过、之后零帧 → 永挂。"""
    _fast_windows(monkeypatch, quiet=0.01)
    monkeypatch.setattr(arb_mod, "SETTLE_RECHECK_DEBOUNCE_SEC", 0.05)
    h = _Harness(settled_seq=[False, False, False, False, True, True])
    arb = h.build()
    await arb.propose({"source": ProposalSource.SETTLE})
    await asyncio.sleep(0.08)  # 续窗耗尽（0.01 + 3×0.01），标记保留
    assert h.terminals() == []
    # 帧簇同批到达（两帧间无 await），落定发生在尾帧之后、补评之前
    await arb.note_state_frame()
    await arb.note_state_frame()
    await asyncio.sleep(0.12)  # trailing 0.05 补评 True → 静默窗收口
    terminals = h.terminals()
    assert len(terminals) == 1
    assert "error" not in terminals[0]
    arb.close()


@pytest.mark.asyncio
async def test_recheck_transient_error_keeps_flag(monkeypatch) -> None:
    """瞬时读失败（非 TeamNotEstablishedError）：只告警保标记，不外抛、
    不判死；下一帧再评可收口。"""
    _fast_windows(monkeypatch, quiet=0.01)
    monkeypatch.setattr(arb_mod, "SETTLE_RECHECK_DEBOUNCE_SEC", 0.02)
    h = _Harness(
        settled_seq=[False, False, False, False, RuntimeError("db locked"), True, True]
    )
    arb = h.build()
    await arb.propose({"source": ProposalSource.SETTLE})
    await asyncio.sleep(0.08)  # 静默窗耗尽（False×3），序列前 4 格已消费
    assert h.terminals() == []
    await arb.note_state_frame()
    await asyncio.sleep(0.05)  # trailing 补评撞瞬时异常
    assert arb.recheck_pending is True  # 标记保留
    assert h.terminals() == []  # 不误判死
    await arb.note_state_frame()  # 下一帧再评 → True → 补调度静默窗收口
    await asyncio.sleep(0.08)
    terminals = h.terminals()
    assert len(terminals) == 1
    assert "error" not in terminals[0]
    arb.close()


@pytest.mark.asyncio
async def test_not_established_degraded_soft_close(monkeypatch) -> None:
    _fast_windows(monkeypatch)
    h = _Harness(
        settled_seq=[TeamNotEstablishedError("no team row")],
        degraded=True,
        degraded_detail="build_team 熔断降级",
    )
    arb = h.build()
    arb.note_leader_output("单干交付的正文")
    # 撞未建立不立即判（degraded 告警帧可能晚于 final）——进静默窗
    assert await arb.propose({"source": ProposalSource.SETTLE}) == Outcome.DEFERRED
    await asyncio.sleep(0.05)  # 窗口首检进四岔
    terminals = h.terminals()
    assert len(terminals) == 1
    assert "error" not in terminals[0]  # 有产出 + 无错误史 → 干净软收口
    assert h.cascades == []  # 软收口不收流


@pytest.mark.asyncio
async def test_not_established_dissolved_soft_close(monkeypatch) -> None:
    _fast_windows(monkeypatch)
    h = _Harness(settled_seq=[TeamNotEstablishedError("no team row")])
    arb = h.build()
    arb.note_team_activity()  # 本轮见过团队活动 → 曾建立后解散
    arb.note_build_attempt()
    arb.note_leader_output("正文")
    assert await arb.propose({"source": ProposalSource.SETTLE}) == Outcome.DEFERRED
    await asyncio.sleep(0.05)
    assert len(h.terminals()) == 1
    assert "error" not in h.terminals()[0]
    assert h.cascades == []


@pytest.mark.asyncio
async def test_not_established_solo_round_soft_close(monkeypatch) -> None:
    _fast_windows(monkeypatch)
    h = _Harness(settled_seq=[TeamNotEstablishedError("no team row")])
    arb = h.build()
    # 未调 build_team、无团队活动、无降级 → 纯单干轮
    arb.note_leader_output("正文")
    assert await arb.propose({"source": ProposalSource.SETTLE}) == Outcome.DEFERRED
    await asyncio.sleep(0.05)
    assert len(h.terminals()) == 1
    assert h.cascades == []


@pytest.mark.asyncio
async def test_not_established_never_built_error_with_cascade(monkeypatch) -> None:
    _fast_windows(monkeypatch)
    h = _Harness(settled_seq=[TeamNotEstablishedError("team never built")])
    arb = h.build()
    arb.note_build_attempt()  # 尝试过建团但彻底失败 → 第四岔判死
    arb.note_leader_output("正文")
    assert await arb.propose({"source": ProposalSource.SETTLE}) == Outcome.DEFERRED
    await asyncio.sleep(0.05)
    assert h.terminals()[0]["error"] == "team never built"
    assert h.cascades == ["settle-team-not-established"]


@pytest.mark.asyncio
async def test_soft_close_zero_content_attaches_error(monkeypatch) -> None:
    """软收口但本轮零正文产出 → 空回合不得记已完成（error 文案复用
    降级告警详情）。"""
    _fast_windows(monkeypatch)
    h = _Harness(
        settled_seq=[TeamNotEstablishedError("no team row")],
        degraded=True,
        degraded_detail="build_team 熔断降级详情",
    )
    arb = h.build()
    assert await arb.propose({"source": ProposalSource.SETTLE}) == Outcome.DEFERRED
    await asyncio.sleep(0.05)
    assert h.terminals()[0]["error"] == "build_team 熔断降级详情"
    assert h.cascades == []


@pytest.mark.asyncio
async def test_stream_end_not_established_no_cascade() -> None:
    """流末四岔：流已尽——第四岔带 error 判死但不级联（与现行强制补发
    同口径）。"""
    h = _Harness(settled_seq=[TeamNotEstablishedError("team never built")])
    arb = h.build()
    arb.note_build_attempt()
    arb.note_leader_output("正文")
    outcome = await arb.propose({"source": ProposalSource.STREAM_END})
    assert outcome == Outcome.EMITTED_ERROR
    assert h.cascades == []


@pytest.mark.asyncio
async def test_stream_end_settled_clean_terminal() -> None:
    h = _Harness(settled_seq=[True])
    arb = h.build()
    outcome = await arb.propose({"source": ProposalSource.STREAM_END})
    assert outcome == Outcome.EMITTED_CLEAN
    assert "error" not in h.terminals()[0]


@pytest.mark.asyncio
async def test_stream_end_unknown_forced_error_terminal() -> None:
    """流末复核 False/None = 状态不明 → 带 error 补发（不粉饰）。"""
    h = _Harness(settled_seq=[False])
    arb = h.build()
    arb.note_leader_output("正文")
    outcome = await arb.propose({"source": ProposalSource.STREAM_END})
    assert outcome == Outcome.EMITTED_ERROR
    assert h.terminals()[0]["error"] == arb_mod.UNCONFIRMED_FINAL_STATE_TEXT


@pytest.mark.asyncio
async def test_stream_end_suppressed_by_pending_pause_cancel() -> None:
    # 挂起交互
    h = _Harness()
    arb = h.build()
    arb.set_pending_interaction(turn=1)
    assert await arb.propose({"source": ProposalSource.STREAM_END}) == Outcome.SUPPRESSED
    # pause 进行中
    h2 = _Harness(pause=True)
    arb2 = h2.build()
    assert await arb2.propose({"source": ProposalSource.STREAM_END}) == Outcome.SUPPRESSED
    # 流取消
    h3 = _Harness()
    arb3 = h3.build()
    arb3.note_stream_cancelled()
    assert await arb3.propose({"source": ProposalSource.STREAM_END}) == Outcome.SUPPRESSED
    assert h.terminals() == h2.terminals() == h3.terminals() == []


@pytest.mark.asyncio
async def test_stream_end_cancels_pending_quiet_window(monkeypatch) -> None:
    """流尽 = 回合已了：挂起的静默窗不再等，立即复核补裁决（动态侧）。"""
    _fast_windows(monkeypatch, quiet=60)
    h = _Harness(settled_seq=[True, True])
    arb = h.build()
    await arb.propose({"source": ProposalSource.SETTLE})
    assert arb.quiet_window_pending is True
    outcome = await arb.propose({"source": ProposalSource.STREAM_END})
    assert outcome == Outcome.EMITTED_CLEAN
    await asyncio.sleep(0.01)  # 取消投递需要一个事件循环节拍
    assert arb.quiet_window_pending is False
    assert len(h.terminals()) == 1


@pytest.mark.asyncio
async def test_death_probe_error_with_cascade() -> None:
    h = _Harness()
    arb = h.build()
    outcome = await arb.propose(
        {"source": ProposalSource.DEATH_PROBE, "error": "leader 重试耗尽"}
    )
    assert outcome == Outcome.EMITTED_ERROR
    assert h.terminals()[0]["error"] == "leader 重试耗尽"
    assert h.cascades == ["leader-death-probe"]


@pytest.mark.asyncio
async def test_stall_watchdog_error_with_cascade() -> None:
    h = _Harness()
    arb = h.build()
    outcome = await arb.propose(
        {"source": ProposalSource.STALL_WATCHDOG, "error": "团队任务停摆"}
    )
    assert outcome == Outcome.EMITTED_ERROR
    assert h.cascades == ["stall-watchdog"]


@pytest.mark.asyncio
async def test_interact_failed_error_no_cascade() -> None:
    h = _Harness()
    arb = h.build()
    outcome = await arb.propose(
        {"source": ProposalSource.INTERACT_FAILED, "error": "pause 进行中，消息被拒绝"}
    )
    assert outcome == Outcome.EMITTED_ERROR
    assert h.terminals()[0]["error"] == "pause 进行中，消息被拒绝"
    assert h.cascades == []


@pytest.mark.asyncio
async def test_settle_suppressed_by_pause_and_pending(monkeypatch) -> None:
    """豁免闸在发帧时点复查（现行 _emit_settle_terminal 同口径，不在提案
    时点）：pause/挂起下提案照常进窗（DEFERRED），窗口到点被闸否决零发帧
    ——挂起可能在窗口内解除、pause 可能在窗口内开始，设闸时点贴近发帧。"""
    _fast_windows(monkeypatch)
    h = _Harness(pause=True)
    arb = h.build()
    assert await arb.propose({"source": ProposalSource.SETTLE}) == Outcome.DEFERRED
    await asyncio.sleep(0.05)
    assert h.terminals() == []
    h2 = _Harness()
    arb2 = h2.build()
    arb2.set_pending_interaction(turn=1)
    assert await arb2.propose({"source": ProposalSource.SETTLE}) == Outcome.DEFERRED
    await asyncio.sleep(0.05)
    assert h2.terminals() == []


@pytest.mark.asyncio
async def test_all_proposals_suppressed_when_stream_gone(monkeypatch) -> None:
    """收窄钉案：提案入口不再全源拦——流内提案
    存活自证（帧在流上到达=流活着），恒真纯冗余；出带提案（探针/看门狗）
    检测层已自闸。存活闸保留在发帧时点与后台任务：本例验证静默窗在流
    终结后 fire 不发帧。"""
    _fast_windows(monkeypatch)
    h = _Harness(stream_alive=False, settled_seq=[True])
    arb = h.build()
    # settle 提案受理（入口不拦），但窗口 fire 时流已死 → 零终态
    assert await arb.propose({"source": ProposalSource.SETTLE}) == Outcome.DEFERRED
    await asyncio.sleep(0.05)
    assert h.terminals() == []


@pytest.mark.asyncio
async def test_concurrent_emitters_single_terminal() -> None:
    """发终态源在各自读数 await 后同时到达发帧点——入口去重
    闸的检查早已过期，无发帧点占位则双终态漏出。修后：先到达者发帧，
    后来者 SUPPRESSED；广播异常释放占位可重试。"""
    gate = asyncio.Event()
    broadcasted: list[dict] = []

    async def _slow_broadcast(payload: dict) -> None:
        await gate.wait()
        broadcasted.append(payload)

    async def _unsettled():
        return None

    arb = RoundTerminalArbiter(
        session_id="s1",
        round_id=1,
        broadcast=_slow_broadcast,
        settled_check=_unsettled,
        has_stream_task=lambda: True,
        is_pause_in_progress=lambda: False,
        mark_round_terminal=lambda: None,
        clear_round_terminal=lambda: None,
    )
    t1 = asyncio.create_task(arb.propose({"source": ProposalSource.TEAM_COMPLETED}))
    await asyncio.sleep(0)  # t1 进入 broadcast await（占位已置位）
    t2 = asyncio.create_task(
        arb.propose({"source": ProposalSource.INTERACT_FAILED, "error": "x"})
    )
    await asyncio.sleep(0)  # t2 撞占位 → SUPPRESSED
    gate.set()
    r1, r2 = await asyncio.gather(t1, t2)
    assert {r1, r2} == {Outcome.EMITTED_CLEAN, Outcome.SUPPRESSED}
    assert len(broadcasted) == 1
    assert arb.completion_signals == 1


@pytest.mark.asyncio
async def test_emit_failure_releases_occupancy() -> None:
    """广播异常释放占位且不计信号——回合不被误关，可重试。"""
    calls = {"n": 0}
    broadcasted: list[dict] = []

    async def _flaky_broadcast(payload: dict) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        broadcasted.append(payload)

    async def _unsettled():
        return None

    arb = RoundTerminalArbiter(
        session_id="s1",
        round_id=1,
        broadcast=_flaky_broadcast,
        settled_check=_unsettled,
        has_stream_task=lambda: True,
        is_pause_in_progress=lambda: False,
        mark_round_terminal=lambda: None,
        clear_round_terminal=lambda: None,
    )
    with pytest.raises(RuntimeError):
        await arb.propose({"source": ProposalSource.TEAM_COMPLETED})
    assert arb.completion_signals == 0  # 未误关
    # 重试成功
    outcome = await arb.propose({"source": ProposalSource.TEAM_COMPLETED})
    assert outcome == Outcome.EMITTED_CLEAN
    assert len(broadcasted) == 1


@pytest.mark.asyncio
async def test_quiet_window_emit_failure_never_raises(
        monkeypatch: pytest.MonkeyPatch,
) -> None:
    """静默窗 fire 发帧时广播异常：后台任务只告警不外抛（防
    never-retrieved），占位已释放、信号未递增，回合未误关。"""
    _fast_windows(monkeypatch)
    h = _Harness(settled_seq=[False, True])  # final 当口 False 挂窗；窗口首检 True
    arb = h.build()

    async def _failing_broadcast(_payload: dict) -> None:
        raise RuntimeError("channel closed")

    arb._broadcast = _failing_broadcast
    outcome = await arb.propose({"source": ProposalSource.SETTLE})
    assert outcome == Outcome.DEFERRED
    task = arb._quiet_task
    assert task is not None
    await task  # 修复前此处 RuntimeError 外抛成 never-retrieved
    assert task.exception() is None
    assert arb.completion_signals == 0
    arb.close()


@pytest.mark.asyncio
async def test_recheck_emit_failure_never_raises(
        monkeypatch: pytest.MonkeyPatch,
) -> None:
    """trailing 补评撞 TeamNotEstablishedError 进四岔发帧时广播异常：
    同样只告警不外抛。"""
    _fast_windows(monkeypatch)
    h = _Harness(settled_seq=[False])
    arb = h.build()

    async def _failing_broadcast(_payload: dict) -> None:
        raise RuntimeError("channel closed")

    arb._broadcast = _failing_broadcast
    await arb.propose({"source": ProposalSource.SETTLE})
    assert arb.recheck_pending is True

    async def _not_established():
        raise TeamNotEstablishedError("team_info has no row")

    arb._settled_check = _not_established
    await arb.note_state_frame()
    task = arb._recheck_task
    assert task is not None
    await task  # 修复前此处 RuntimeError 外抛
    assert task.exception() is None
    assert arb.completion_signals == 0
    arb.close()


@pytest.mark.asyncio
async def test_stream_end_forced_suppressed_when_pause_starts_during_settle_read() -> None:
    """流末 settle 补发的读数 await 期间用户开始暂停：强制补发分支
    必须复查豁免，不得把「已停止」补成带 error 的终态帧。"""
    h = _Harness(settled_seq=[False, False, False])
    arb = h.build()

    calls = {"n": 0}
    orig_check = arb._settled_check

    async def _flipping_check():
        calls["n"] += 1
        result = await orig_check()
        if calls["n"] == 2:
            h._pause = True  # settle 补发读数期间用户点了停止
        return result

    arb._settled_check = _flipping_check

    outcome1 = await arb.propose({"source": ProposalSource.SETTLE})
    assert outcome1 == Outcome.DEFERRED  # call#1 False → 挂窗
    outcome2 = await arb.propose({"source": ProposalSource.STREAM_END})
    # 入口 pause=False 放行 → settle 补发 call#2 False（pause 在此期间开始）
    # → 修复前落入强制补发 call#3 后发 error 终帧；修复后复查命中豁免
    assert outcome2 == Outcome.SUPPRESSED
    assert h.terminals() == []
    assert arb.completion_signals == 0
    arb.close()
