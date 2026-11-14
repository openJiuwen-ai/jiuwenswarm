# coding: utf-8
"""回合终态裁决器（RoundTerminalArbiter）。

要点：
- settle 系的全部豁免闸（去重/流存活/挂起交互/pause）在**发帧时点**
  复查，不在提案时点——现行 `_emit_settle_terminal` 同口径：静默窗
  30-50s 期间用户点停止是常态，挂起也可能在窗口内解除；
- settle 提案撞 TeamNotEstablishedError **不立即判**，进静默窗等一拍：
  降级轮的 degraded 告警帧在运行时 IDLE 时才发出，
  可能晚于 final 到达——窗口首检进四岔时读数已全；
- 级联 source 字符串与现行一致：leader-death-probe / stall-watchdog /
  settle-team-not-established；
- 事件驱动重评不内联 await 在帧消费路径（读数含
  DB 点查，锁竞争/读池饱和下可阻塞调用方数秒，观感同族于挂死）——
  `note_state_frame` 只调度后台补评任务；去抖由补评任务内部新鲜度
  检查保证（0.5s 内只评一次），帧簇尾帧携带的决定性迁移不丢
  （leading-edge 去抖丢尾帧 + 之后零帧 = 永挂再现）；
  瞬时读失败只告警保标记；后台任务的发帧/打标阶段同口径只告警不外抛
  （后台 task 异常绝不外抛成 never-retrieved，帧未送达则回合未误关，
  流末兜底仍可收口）。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, TypedDict

from openjiuwen.core.common.logging import logger

from jiuwenswarm.agents.harness.team.handlers.team_monitor_handler import (
    TeamNotEstablishedError,
)

# settle 终态延迟静默确认窗（秒）：窗内有新内容帧即作废
SETTLE_QUIET_SEC = 2.0
# 读数未就绪（None）续窗重试上限
SETTLE_NONE_MAX_ATTEMPTS = 3
# 确未落定（False）有界续窗：final 当口的 False 多为 leader 收尾
# 迁移落库前的毫秒级时序差；持续 False = 确有在途工作，耗尽退回旧行为不强制
# 收口（防误杀成员长模型调用合法静默，PM-53 实测单调用 300s 级）
SETTLE_FALSE_RECHECK_SEC = 15.0
SETTLE_FALSE_MAX_ATTEMPTS = 8
# 事件驱动重评去抖：落定过程状态帧密集，读数点查不必每帧一次
SETTLE_RECHECK_DEBOUNCE_SEC = 0.5

# 终态文案
UNCONFIRMED_FINAL_STATE_TEXT = "Team round ended with unconfirmed final state"
NO_DELIVERABLE_CONTENT_TEXT = "Team round ended with no deliverable content"


class ProposalSource:
    """收尾路径的提案源标识。"""

    TEAM_COMPLETED = "team_completed"
    INSTANT_FINAL = "instant_final"
    SETTLE = "settle"
    DEATH_PROBE = "death_probe"
    STALL_WATCHDOG = "stall_watchdog"
    STREAM_END = "stream_end"
    INTERACT_FAILED = "interact_failed"


class Outcome:
    """裁决返回值。"""

    EMITTED_CLEAN = "emitted_clean"  # 干净终态（可带统计/错误史）
    EMITTED_SOFT = "emitted_soft"  # 软收口（降级/解散/纯单干，不收流）
    EMITTED_ERROR = "emitted_error"  # 带 error 终态（判死/状态不明/投递失败）
    SUPPRESSED = "suppressed"  # 被前置闸否决（去重/豁免命中/窗口作废）
    DEFERRED = "deferred"  # 进静默窗（settle 类提案的延迟确认）


class TerminalProposal(TypedDict, total=False):
    """收尾提案：提案方只带证据，不自带逻辑。"""

    source: str  # ProposalSource 之一
    error: Optional[str]  # 提案方已知错误史/判死原因
    hint: Optional[str]  # settle 类提案预检提示（保留位）
    member_count: Optional[int]  # team_completed 统计（干净帧携带）
    task_count: Optional[int]


@dataclass
class RoundLedger:
    """流内账本——随（会话，流实例）生灭。

    只收编终态判定需要的轮级判据；leader 活性/全量流帧等检测层计数不在此
    （探针/看门狗自持）。
    """

    content_chunks: int = 0  # 内容帧计数（静默窗作废判据）
    completion_signals: int = 0  # 终态信号计数（去重唯一事实源，可再武装）
    unrecovered_error: Optional[str] = None  # 本轮未恢复 leader 错误史
    had_answer_content: bool = False  # 本轮产出标记（零产出防粉饰）
    pending_interaction: bool = False  # 挂起交互标记
    pending_interaction_turn: int = -1
    stream_cancelled: bool = False  # 流取消标记
    saw_team_activity: bool = False  # 本轮团队活动（dissolved 判据）
    saw_build_attempt: bool = False  # 本轮建团尝试（单干判据）


class RoundTerminalArbiter:
    """回合终态裁决器：每（session，流实例）一个，随流回收（`aclose()`）。

    注入（全部为无业务逻辑的访问链）：
    - ``broadcast``：终态帧广播；
    - ``settled_check``：settle 四件套读数，返回 True/False/None，
      撞确定性终结抛 ``TeamNotEstablishedError``（R9）；
    - ``has_stream_task`` / ``is_pause_in_progress``：存活 / 停止豁免；
    - ``mark_round_terminal`` / ``clear_round_terminal``：打标与清除；
    - ``cascade``：级联收流（收 source 字符串）；
    - ``has_session_degraded`` / ``session_degraded_detail``：四岔降级岔与
      零产出 error 文案。
    """

    def __init__(
            self,
            *,
            session_id: str,
            round_id: Any,
            broadcast: Callable[[dict], Awaitable[None]],
            settled_check: Callable[[], Awaitable[bool | None]],
            has_stream_task: Callable[[], bool],
            is_pause_in_progress: Callable[[], bool],
            mark_round_terminal: Callable[[], None],
            clear_round_terminal: Callable[[], None],
            cascade: Callable[[str], None] | None = None,
            has_session_degraded: Callable[[], bool] = lambda: False,
            session_degraded_detail: Callable[[], Optional[str]] = lambda: None,
            settle_quiet_sec: Optional[float] = None,
            settle_none_max_attempts: Optional[int] = None,
            settle_false_recheck_sec: Optional[float] = None,
            settle_false_max_attempts: Optional[int] = None,
            settle_recheck_debounce_sec: Optional[float] = None,
    ) -> None:
        self._session_id = session_id
        self._round_id = round_id
        self._broadcast = broadcast
        self._settled_check = settled_check
        self._has_stream_task = has_stream_task
        self._is_pause_in_progress = is_pause_in_progress
        self._mark_round_terminal = mark_round_terminal
        self._clear_round_terminal = clear_round_terminal
        self._cascade = cascade
        self._has_session_degraded = has_session_degraded
        self._session_degraded_detail = session_degraded_detail
        # 时序配置：None 时取本模块常量（接入点 team_helpers 传入它自己的常量
        self._quiet_sec = (
            settle_quiet_sec if settle_quiet_sec is not None else SETTLE_QUIET_SEC
        )
        self._none_max_attempts = (
            settle_none_max_attempts
            if settle_none_max_attempts is not None
            else SETTLE_NONE_MAX_ATTEMPTS
        )
        self._false_recheck_sec = (
            settle_false_recheck_sec
            if settle_false_recheck_sec is not None
            else SETTLE_FALSE_RECHECK_SEC
        )
        self._false_max_attempts = (
            settle_false_max_attempts
            if settle_false_max_attempts is not None
            else SETTLE_FALSE_MAX_ATTEMPTS
        )
        self._recheck_debounce_sec = (
            settle_recheck_debounce_sec
            if settle_recheck_debounce_sec is not None
            else SETTLE_RECHECK_DEBOUNCE_SEC
        )

        self._ledger = RoundLedger()
        self._quiet_task: asyncio.Task | None = None
        # 最终闸占位标志（见 _emit_terminal）：并发提案在各自读数 await
        # 后可同时到达发帧点，入口闸的检查早已过期
        self._emitting = False
        # 本流是否提案过 settle（流末 finally 的 settle 补发尝试判据——
        # 与现行 settle_terminal_task is not None 等价）
        self._settle_proposed = False
        # 事件驱动重评标记与去抖时钟。
        # 生命周期两处清除：终态发射统一复位（_reset_round_marks）与新一轮
        # leader 内容帧再武装（note_content_frame）——跨轮残留会在新轮起跑
        # 间隙的空板快照上误判 True，对新轮发抢跑终态。
        self._recheck_pending = False
        self._last_recheck_at = 0.0
        # trailing 补评任务槽：note_state_frame 只调度不内联（保真要点），
        # 槽位防帧簇连发重复调度；fire 时查标记/流存活/新鲜度，空转返回，
        # 标记清除点无需联动取消。
        self._recheck_task: asyncio.Task | None = None

    def note_content_frame(self, *, is_leader: bool) -> None:
        """内容帧（delta/reasoning/tool_call）到达：计数 + 清标 + 再武装判定。

        反向豁免（认所有内容帧）：终态后旧流上再到新内容帧（团队自发
        续跑/迟到回报唤醒）→ 清回合终态标记，下一条用户消息回归 follow-up
        判定。再武装（只认 leader）：终态后 **leader**
        新内容帧到达 → 去重账本复位、回到开放态（新一轮 leader 回合重新
        获得发终态资格），同时拆除在途静默窗与重评标记（旧轮 settle
        上下文已死，防跨轮抢跑）。成员尾随帧不再武装——成员迟到产出本身
        不产生终态需求，复位反而会让流末兜底给已干净终态的回合再补一条
        带 error 的帧。
        """
        self._ledger.content_chunks += 1
        if self._ledger.completion_signals > 0:
            self._clear_round_terminal()
        if is_leader and self._ledger.completion_signals > 0:
            self._ledger.completion_signals = 0
            self._recheck_pending = False
            self._cancel_quiet_window()
            logger.info(
                "[RoundTerminalArbiter] terminal re-armed: new leader content "
                "after terminal, session_id=%s",
                self._session_id,
            )

    def note_leader_output(self, text: str) -> None:
        """leader 产出实质正文：此前的模型错误视为已恢复（清错误史），
        并置本轮产出标记（零产出校验用）。"""
        if not text.strip():
            return
        self._ledger.unrecovered_error = None
        self._ledger.had_answer_content = True

    def note_leader_error(self, error: str) -> None:
        """记录本轮未恢复的 leader 模型错误史（强制携带用）。

        重试通知与 degraded 告警的过滤在检测层（消费循环），裁决器不过滤。
        """
        self._ledger.unrecovered_error = error

    def note_team_activity(self) -> None:
        """本轮见过团队活动帧（team.member/team.task/workflow.updated）——
        四岔 dissolved 判定的本轮 scope 判据。"""
        self._ledger.saw_team_activity = True

    def note_build_attempt(self) -> None:
        """本轮 leader 调过 build_team（四岔纯单干判据）。"""
        self._ledger.saw_build_attempt = True

    def set_pending_interaction(self, turn: int) -> None:
        """leader 向用户发问 = 回合挂起等输入；快照用户轮次号供
        epoch 门判定（清除只认「作答后」的恢复产出——判定在消费循环）。"""
        self._ledger.pending_interaction = True
        self._ledger.pending_interaction_turn = turn

    def clear_pending_interaction(self) -> None:
        """恢复产出到达（epoch 门已由消费循环判定）→ 清除挂起标记。"""
        self._ledger.pending_interaction = False

    def note_stream_cancelled(self) -> None:
        """流被取消：标记 + 回收静默窗。"""
        self._ledger.stream_cancelled = True
        self._cancel_quiet_window()

    async def note_state_frame(self) -> None:
        """team.member/team.task/workflow.updated 帧到达时的重评入口。

        仅在 settle False 置标后生效。**不内联重评**（读数含 DB 点查，
        锁竞争/读池饱和下可阻塞调用方数秒）：有标记且无
        在途补评任务即调度 trailing 位；去抖由补评任务内部新鲜度检查
        保证（0.5s 内只评一次），帧簇尾帧携带的决定性迁移不丢。
        """
        if not self._recheck_pending:
            return
        if self._recheck_task is None or self._recheck_task.done():
            self._recheck_task = asyncio.create_task(
                self._trailing_recheck(),
                name=f"arbiter-settle-recheck-{self._session_id}",
            )

    async def _trailing_recheck(self) -> None:
        """trailing 补评：睡满一个去抖窗后重评。fire 时标记已清（终态已发/
        再武装）、流已不在、或刚评过，均空转返回。"""
        try:
            await asyncio.sleep(self._recheck_debounce_sec)
        except asyncio.CancelledError:
            return
        if not self._recheck_pending:
            return
        if not self._has_stream_task():
            return
        if time.monotonic() - self._last_recheck_at < self._recheck_debounce_sec:
            return
        await self._do_recheck()

    async def _do_recheck(self) -> None:
        """事件驱动重评单份实现。True → 静默窗已在排队则
        交给它、否则补调度；False/None → 保持标记等下一帧（定时器续窗
        并行守）；撞 TeamNotEstablishedError → 与静默窗同口径立即进四岔。
        瞬时读失败只告警保标记——跑在后台 task 里，异常绝不外抛成
        never-retrieved 雷。"""
        self._last_recheck_at = time.monotonic()
        try:
            result = await self._settled_check()
        except TeamNotEstablishedError:
            self._recheck_pending = False
            # 经统一出口（发帧点四闸复查 + 四岔），与现行事件钩子同口径
            await self._emit_settle_terminal_guarded(context="recheck not-established")
            return
        except Exception as exc:
            logger.warning(
                "[RoundTerminalArbiter] settle event-driven recheck failed: "
                "session_id=%s round_id=%s error=%s",
                self._session_id,
                self._round_id,
                exc,
            )
            return
        logger.info(
            "[RoundTerminalArbiter] settle event-driven recheck: "
            "session_id=%s round_id=%s result=%s",
            self._session_id,
            self._round_id,
            result,
        )
        if result is True:
            # 标记保留至终态发射——窗口若被新内容帧作废，后续状态帧仍可重评
            if self._quiet_task is None or self._quiet_task.done():
                self._schedule_quiet_window()
        # False（确有在途）/None（读数未就绪）：保持标记；定时器续窗并行守

    async def propose(self, proposal: TerminalProposal) -> str:
        """裁决入口。前置闸按提案源适配（去重全源生效；停止 / 挂起
        仅 settle 系与 stream_end； 取消仅 stream_end）。

        存活闸不在入口全源拦：流内提案
        （team_completed/instant_final/interact_failed/final 当口 settle）
        到达即存活自证，恒真纯冗余；出带提案（探针/看门狗）检测层已自闸。
        存活复查保留在发帧时点（_emit_settle_terminal）与后台任务
        （静默窗/trailing 补评）——它们可能在流终结后 fire。"""
        source = proposal["source"]
        # 去重：本轮已发过终态 → 一律否决（再武装见 note_content_frame）
        if self._ledger.completion_signals > 0:
            return Outcome.SUPPRESSED

        if source == ProposalSource.TEAM_COMPLETED:
            # SDK 真值直信 + 错误史携带；统计键恒在（值可 None，
            # 与现行 team.completed 分支帧形状一致）
            stats: dict[str, Any] = {}
            if "member_count" in proposal:
                stats["member_count"] = proposal["member_count"]
            if "task_count" in proposal:
                stats["task_count"] = proposal["task_count"]
            return await self._emit_terminal(
                error=self._ledger.unrecovered_error,
                stats=stats or None,
                soft=False,
                cascade_source=None,
                log_reason="team_completed",
            )

        if source == ProposalSource.INSTANT_FINAL:
            # 纯文本/workflow 完成即时收尾（should_finish 门控在消费循环）
            return await self._emit_terminal(
                error=self._ledger.unrecovered_error,
                stats=None,
                soft=False,
                cascade_source=None,
                log_reason="instant_final",
            )

        if source == ProposalSource.INTERACT_FAILED:
            # 投递失败：已广播的 chat.error 同文案收尾（防"失败记成已完成"）
            return await self._emit_terminal(
                error=proposal.get("error") or UNCONFIRMED_FINAL_STATE_TEXT,
                stats=None,
                soft=False,
                cascade_source=None,
                log_reason="interact_failed",
            )

        if source in (ProposalSource.DEATH_PROBE, ProposalSource.STALL_WATCHDOG):
            # 判死类：检测层自带条件（活性基线/任务判活/kernel 活性），
            # 裁决器只收口——带 error + 级联收流
            return await self._emit_terminal(
                error=proposal.get("error") or UNCONFIRMED_FINAL_STATE_TEXT,
                stats=None,
                soft=False,
                cascade_source=(
                    "leader-death-probe"
                    if source == ProposalSource.DEATH_PROBE
                    else "stall-watchdog"
                ),
                log_reason=source,
            )

        if source == ProposalSource.SETTLE:
            # 提案时不设闸（现行同口径：final 当口只评估+挂窗，去重/挂起/
            # pause 豁免全部在发帧时点复查——挂起可能在窗口内解除、pause
            # 可能在窗口内开始，设闸时点必须贴近发帧时点）
            self._settle_proposed = True
            try:
                result = await self._settled_check()
            except TeamNotEstablishedError as e:
                # 确定性终结——但不立即判：降级轮的 degraded 告警帧在
                # 运行时 IDLE 时才发出，可能晚于 final 到达。
                # 进静默窗等一拍，窗口首检进四岔（发帧点豁免闸同样复查）
                logger.info(
                    "[RoundTerminalArbiter] settle decision at final: "
                    "session_id=%s round_id=%s team not established: %s",
                    self._session_id,
                    self._round_id,
                    e,
                )
                self._schedule_quiet_window()
                return Outcome.DEFERRED
            # 日志措辞保留现行「settle decision at final」锚点
            logger.info(
                "[RoundTerminalArbiter] settle decision at final: "
                "session_id=%s round_id=%s decision=%s",
                self._session_id,
                self._round_id,
                result,
            )
            if result is False:
                # 确未落定不再裸赌 team.completed——置事件驱动重评
                # 标记，静默窗按 False 预算续评
                self._recheck_pending = True
            self._schedule_quiet_window()
            return Outcome.DEFERRED

        if source == ProposalSource.STREAM_END:
            # 豁免（现行流末 finally 同口径）
            if self._ledger.stream_cancelled:
                return Outcome.SUPPRESSED
            if self._ledger.pending_interaction:
                return Outcome.SUPPRESSED
            if self._is_pause_in_progress():
                logger.info(
                    "[RoundTerminalArbiter] stream-end terminal skipped "
                    "(pause in progress): session_id=%s round_id=%s",
                    self._session_id,
                    self._round_id,
                )
                return Outcome.SUPPRESSED
            # 流尽 = 回合已了：静默窗不再等（动态侧）
            self._cancel_quiet_window()
            # 与现行 finally 同序：本流评估过 settle 先试 settle 补发
            # （发帧点四闸复查 + 复核 + 撞未建立进四岔——settle 口径允许级联）
            if self._settle_proposed:
                outcome = await self._emit_settle_terminal()
                if outcome != Outcome.SUPPRESSED:
                    return outcome
                if self._ledger.completion_signals > 0:
                    return Outcome.SUPPRESSED
                # settle 补发被否决的原因不可区分（pause/挂起/读数非 True
                # 都可能）——豁免条件可能在其读数 await 期间新发生，强制
                # 补发前复查（设闸时点必须贴近发帧时点）：命中则不发帧，
                # 防「已停止/待作答」被强制补成完成或失败
                if self._ledger.pending_interaction:
                    return Outcome.SUPPRESSED
                if self._is_pause_in_progress():
                    logger.info(
                        "[RoundTerminalArbiter] stream-end forced terminal "
                        "skipped (pause in progress): session_id=%s "
                        "round_id=%s",
                        self._session_id,
                        self._round_id,
                    )
                    return Outcome.SUPPRESSED
            # 强制补发（现行 _emit_forced_terminal_at_stream_end 口径）：
            # 复核 True（确证落定只是帧丢了）补干净（照挂错误史）；
            # False/None 状态不明补带 error（不粉饰，错误史优先）；
            # 撞未建立按 forced 口径——软岔干净收口、第四岔带 error 但
            # 流已尽不级联
            try:
                verdict = await self._settled_check()
            except TeamNotEstablishedError as e:
                return await self._resolve_not_established(e, cascade_allowed=False)
            if verdict is True:
                return await self._emit_terminal(
                    error=self._ledger.unrecovered_error,
                    stats=None,
                    soft=False,
                    cascade_source=None,
                    log_reason="stream_end_settled",
                )
            return await self._emit_terminal(
                error=(
                        self._ledger.unrecovered_error
                        or UNCONFIRMED_FINAL_STATE_TEXT
                ),
                stats=None,
                soft=False,
                cascade_source=None,
                log_reason="stream_end_forced",
            )

        logger.warning(
            "[RoundTerminalArbiter] unknown proposal source %r: session_id=%s",
            source,
            self._session_id,
        )
        return Outcome.SUPPRESSED

    async def _resolve_not_established(
            self, e: TeamNotEstablishedError, *, cascade_allowed: bool
    ) -> str:
        """撞 TeamNotEstablishedError 的四岔收口：

        - 有降级标记 → 完成+告警（干净帧，不收流——运行时活着，追问可用）；
        - 无降级但本轮见过团队活动 → 解散轮（同上）；
        - 本轮未调 build_team → 纯单干轮（同上——「未建立」是正常形态）；
        - 三者都不是 → 从未建立：带 error 判死，settle 路径级联收流
          （流末路径流已尽，不级联——``cascade_allowed=False``）。
        软收口约束：零正文产出的空回合不得记已完成。
        """
        degraded = self._has_session_degraded()
        dissolved = not degraded and self._ledger.saw_team_activity
        solo = not degraded and not dissolved and not self._ledger.saw_build_attempt
        if degraded or dissolved or solo:
            error = self._ledger.unrecovered_error
            if not error and not self._ledger.had_answer_content:
                error = (
                        self._session_degraded_detail()
                        or NO_DELIVERABLE_CONTENT_TEXT
                )
            kind = (
                "degraded round"
                if degraded
                else ("team dissolved" if dissolved else "solo round")
            )
            return await self._emit_terminal(
                error=error,
                stats=None,
                soft=True,
                cascade_source=None,
                log_reason=f"not established ({kind})",
            )
        return await self._emit_terminal(
            error=str(e),
            stats=None,
            soft=False,
            cascade_source=(
                "settle-team-not-established" if cascade_allowed else None
            ),
            log_reason="not established (never built)",
        )

    async def _emit_settle_terminal(self) -> str:
        """发帧前复查四闸（去重/流存活/挂起交互/pause）→ 复核 settle →
        True 发干净终态（挂错误史）；撞 TeamNotEstablishedError 进
        四岔（settle 口径允许级联）；其余否决。

        设闸时点必须贴近发帧时点：静默窗 30-50s 期间用户点停止是常态
        （此时 settle 复核必然非 True，补出去的一定是带 error 的
        误伤帧，「已停止」被翻成「失败」）；挂起交互也可能在窗口内解除。
        """
        if self._ledger.completion_signals > 0:
            return Outcome.SUPPRESSED
        if not self._has_stream_task():
            return Outcome.SUPPRESSED
        if self._ledger.pending_interaction:
            return Outcome.SUPPRESSED
        if self._is_pause_in_progress():
            logger.info(
                "[RoundTerminalArbiter] settle terminal skipped "
                "(pause in progress): session_id=%s round_id=%s",
                self._session_id,
                self._round_id,
            )
            return Outcome.SUPPRESSED
        try:
            settled = await self._settled_check()
        except TeamNotEstablishedError as e:
            return await self._resolve_not_established(e, cascade_allowed=True)
        if settled is not True:
            return Outcome.SUPPRESSED
        return await self._emit_terminal(
            error=self._ledger.unrecovered_error,
            stats=None,
            soft=False,
            cascade_source=None,
            log_reason="settle",
        )

    async def _emit_settle_terminal_guarded(self, *, context: str) -> None:
        """后台任务（静默窗/trailing 补评）专用发帧包装：广播/打标阶段
        异常只告警不外抛——后台 task 异常绝不外抛成 never-retrieved；
        帧未送达则 completion_signals 未递增、占位已释放，回合未误关，
        后续事件重评与流末兜底仍可收口。"""
        try:
            await self._emit_settle_terminal()
        except Exception:
            logger.warning(
                "[RoundTerminalArbiter] settle terminal emit failed (%s): "
                "session_id=%s round_id=%s",
                context,
                self._session_id,
                self._round_id,
                exc_info=True,
            )

    def _schedule_quiet_window(self) -> None:
        """settle 终态延迟静默窗确认：窗口内有新内容帧（团队还在干活）即作废；
        None 续窗重试 SETTLE_NONE_MAX_ATTEMPTS 次，False 按
        SETTLE_FALSE_RECHECK_SEC × SETTLE_FALSE_MAX_ATTEMPTS 续窗重评。"""
        self._cancel_quiet_window()
        baseline_content = self._ledger.content_chunks
        baseline_signals = self._ledger.completion_signals

        async def _delayed() -> None:
            none_attempts = 0
            false_attempts = 0
            while True:
                try:
                    await asyncio.sleep(self._quiet_sec)
                except asyncio.CancelledError:
                    return
                if self._ledger.content_chunks != baseline_content:
                    return
                if self._ledger.completion_signals != baseline_signals:
                    return
                if not self._has_stream_task():
                    return
                try:
                    result = await self._settled_check()
                except TeamNotEstablishedError:
                    # 确定性终结：经统一出口（发帧点四闸复查 + 四岔），
                    # 与现行窗口内 _emit_settle_terminal 同口径
                    await self._emit_settle_terminal_guarded(
                        context="window not-established"
                    )
                    return
                except Exception as exc:
                    # 瞬时读失败（锁竞争/读池抖动）只告警、按读数未就绪
                    # 续窗——后台 task 异常绝不外抛成 never-retrieved
                    logger.warning(
                        "[RoundTerminalArbiter] settle window check read "
                        "failed: session_id=%s round_id=%s error=%s",
                        self._session_id,
                        self._round_id,
                        exc,
                    )
                    result = None
                logger.info(
                    "[RoundTerminalArbiter] settle window check: session_id=%s "
                    "round_id=%s result=%s none_attempts=%s false_attempts=%s",
                    self._session_id,
                    self._round_id,
                    result,
                    none_attempts,
                    false_attempts,
                )
                if result is True:
                    await self._emit_settle_terminal_guarded(context="window settled")
                    return
                if result is None:
                    none_attempts += 1
                    if none_attempts >= self._none_max_attempts:
                        logger.info(
                            "[RoundTerminalArbiter] settle terminal abandoned "
                            "after retries: session_id=%s round_id=%s",
                            self._session_id,
                            self._round_id,
                        )
                        return
                    continue
                # False：确有在途工作，有界续窗——耗尽退回等
                # team.completed 旧行为，不强制收口
                false_attempts += 1
                if false_attempts >= self._false_max_attempts:
                    logger.warning(
                        "[RoundTerminalArbiter] settle still false after %s "
                        "rechecks, falling back to waiting for team.completed: "
                        "session_id=%s round_id=%s",
                        false_attempts,
                        self._session_id,
                        self._round_id,
                    )
                    return
                try:
                    await asyncio.sleep(self._false_recheck_sec)
                except asyncio.CancelledError:
                    return

        self._quiet_task = asyncio.create_task(
            _delayed(), name=f"round-terminal-quiet-{self._session_id}"
        )

    def _cancel_quiet_window(self) -> None:
        if self._quiet_task is not None and not self._quiet_task.done():
            self._quiet_task.cancel()

    async def _emit_terminal(
            self,
            *,
            error: Optional[str],
            stats: Optional[dict],
            soft: bool,
            cascade_source: Optional[str],
            log_reason: str,
    ) -> str:
        payload: dict[str, Any] = {
            "event_type": "chat.processing_status",
            "session_id": self._session_id,
            "rid": self._round_id,
            "is_processing": False,
            "is_complete": True,
        }
        if stats:
            payload.update(stats)
        if error:
            payload["error"] = error
        # 最终闸（同步段占位：检查-置位间无让出点）：并发终态源（静默窗
        # fire × 探针/看门狗 fire × 流末兜底）在各自读数 await 后可同时到达
        # 此处——propose/_emit_settle_terminal 入口的去重检查在 await 之前
        # 早已过期，不在发帧点复查则双终态帧漏出（前端同轮两帧）。
        # 占位即拒后来者优先先到达者；广播异常释放占位并回滚——帧未送达
        # 不得把回合误关（调用方可重试）。
        if self._emitting or self._ledger.completion_signals > 0:
            return Outcome.SUPPRESSED
        self._emitting = True
        try:
            await self._broadcast(payload)
        except BaseException:
            self._emitting = False
            raise
        self._emitting = False
        self._ledger.completion_signals += 1
        self._mark_round_terminal()
        self._reset_round_marks()
        outcome = (
            Outcome.EMITTED_SOFT
            if soft
            else (Outcome.EMITTED_ERROR if error else Outcome.EMITTED_CLEAN)
        )
        logger.info(
            "[RoundTerminalArbiter] terminal emitted (%s): session_id=%s "
            "round_id=%s error=%s",
            log_reason,
            self._session_id,
            self._round_id,
            bool(error),
        )
        if cascade_source and self._cascade is not None:
            # 级联必须独立任务逃生在调用方实现（探针/看门狗会被流收尾
            # finally 取消，自我 await 会打断自己——现行同口径注入回调）
            self._cascade(cascade_source)
        return outcome

    def _reset_round_marks(self) -> None:
        """终态发射点统一复位（回合边界按「终态发射」定义）——重评
        标记一并拆除（回合已收口，旧轮不得再驱动重评）。"""
        ledger = self._ledger
        ledger.had_answer_content = False
        ledger.saw_team_activity = False
        ledger.saw_build_attempt = False
        ledger.pending_interaction = False
        ledger.unrecovered_error = None
        self._recheck_pending = False

    def close(self) -> None:
        """随流回收：取消静默窗与重评补评任务（与现行 finally 回收同口径——
        只 cancel，不 await：任务内部对 CancelledError 即时响应）。"""
        self._cancel_quiet_window()
        if self._recheck_task is not None and not self._recheck_task.done():
            self._recheck_task.cancel()

    @property
    def recheck_pending(self) -> bool:
        """重评标记（测试观测用）。"""
        return self._recheck_pending

    @property
    def has_pending_interaction(self) -> bool:
        """挂起交互标记（消费循环零产出守卫读数用）。"""
        return self._ledger.pending_interaction

    @property
    def quiet_window_pending(self) -> bool:
        """静默窗是否在排队（测试观测用）。"""
        return self._quiet_task is not None and not self._quiet_task.done()

    @property
    def completion_signals(self) -> int:
        """去重账本读数（测试观测用）。"""
        return self._ledger.completion_signals
