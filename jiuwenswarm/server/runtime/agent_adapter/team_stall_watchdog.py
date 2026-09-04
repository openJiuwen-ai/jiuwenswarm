# coding: utf-8
"""团队回合停摆看门狗。

死亡探针（team_helpers._schedule_leader_round_death_probe）只管"leader 模型
错误后零产出"；停摆是另一族：leader 正常收尾（或重试断片后自认无事可做）、
任务已下发但零成员在途——回合无人推进，team.completed 因任务非终态永不成立，
流靠 keepalive 吊命，前端永远"正在思考"。

判定（每个窗口）：流存活 + 无任何流帧活动 + 零在途成员 + 有未终态任务，
连续 _TEAM_STALL_CONFIRM_WINDOWS 个窗口成立 → 广播 processing_status 失败终态。

退化方向与死亡探针一致：快照不可用/有任何活动迹象都按"不停摆"处理。
跨轮存活：收尾信号变化不终止看门狗，只重置计数（长寿命流多轮复用）。

broadcast 由调用方注入（team_helpers._broadcast_event），避免对本模块的
循环依赖（team_helpers 是本模块的调用方）。
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from openjiuwen.core.common.logging import logger

from jiuwenswarm.agents.harness.team import get_team_manager

# 停摆判定窗口与确认次数：连续两窗口成立才判死——任务下发到成员启动有正常
# 延迟，单窗口误杀风险高。窗口需大于成员启动的典型耗时。
TEAM_STALL_WATCHDOG_SEC = 45.0
TEAM_STALL_CONFIRM_WINDOWS = 2

# 任务终态集合：不在其中的都视为"待推进"
TASK_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})

# 判定"成员仍在工作"的执行中状态（MemberStatus.busy 之外的旁证）
# 与 team_helpers._MEMBER_IN_FLIGHT_EXEC_STATUSES 同口径（那边供死亡探针用）
MEMBER_IN_FLIGHT_EXEC_STATUSES = frozenset({"starting", "running", "completing"})

BroadcastFn = Callable[[str | None, str, dict[str, Any]], Awaitable[None]]


async def team_progress_snapshot(
        channel_id: str | None, session_id: str
) -> tuple[int, int] | None:
    """返回 (在途成员数, 未终态任务数)；快照不可用返回 None（调用方按"不停摆"退化）。

    快照源与死亡探针相同（team_monitor_handler.get_team_snapshot；members 已
    剔除 leader）；停摆判定需要同时看成员与任务，一次快照取齐。
    """
    try:
        tm = get_team_manager(channel_id)
        monitor_handler = tm.get_monitor_handler(session_id)
        if monitor_handler is None:
            return None
        snapshot = await monitor_handler.get_team_snapshot()
        if not snapshot:
            return None
        in_flight = 0
        for m in snapshot.get("members", []):
            if (
                m.get("status") == "busy"
                or m.get("execution_status") in MEMBER_IN_FLIGHT_EXEC_STATUSES
            ):
                in_flight += 1
        pending = 0
        for t in snapshot.get("tasks", []):
            if str(t.get("status") or "") not in TASK_TERMINAL_STATUSES:
                pending += 1
        return in_flight, pending
    except Exception:
        logger.debug(
            "[TeamStallWatchdog] team progress snapshot failed: session_id=%s",
            session_id,
            exc_info=True,
        )
        return None


def schedule_team_stall_watchdog(
        channel_id: str | None,
        session_id: str,
        round_id: Any,
        *,
        liveness: Callable[[], int],
        completion_signals: Callable[[], int],
        broadcast: BroadcastFn,
) -> asyncio.Task:
    """启动停摆看门狗，返回任务供调用方持有/随流取消。

    liveness / completion_signals: 零参数可调用，分别返回本流已消费 chunk 数
    （停摆语义的活性口径是全量流帧——任何成员/团队事件都算活动，与死亡探针
    "只数 leader 帧"不同）与已广播的回合收尾信号数。
    """

    async def _watchdog() -> None:
        stall_windows = 0
        last_chunks = liveness()
        last_signals = completion_signals()
        while True:
            try:
                await asyncio.sleep(TEAM_STALL_WATCHDOG_SEC)
            except asyncio.CancelledError:
                return
            try:
                tm = get_team_manager(channel_id)
                if not tm.has_stream_task(session_id):
                    return
                signals = completion_signals()
                if signals != last_signals:
                    # 回合已正常收尾：重置基线继续守下一轮（长寿命流跨轮复用）
                    last_signals = signals
                    stall_windows = 0
                    last_chunks = liveness()
                    continue
                current = liveness()
                if current != last_chunks:
                    last_chunks = current
                    stall_windows = 0
                    continue
                progress = await team_progress_snapshot(channel_id, session_id)
                if progress is None:
                    stall_windows = 0
                    continue
                in_flight, pending = progress
                if in_flight > 0 or pending == 0:
                    stall_windows = 0
                    continue
                stall_windows += 1
                if stall_windows < TEAM_STALL_CONFIRM_WINDOWS:
                    logger.info(
                        "[TeamStallWatchdog] team round stall suspected: channel_id=%s "
                        "session_id=%s round_id=%s pending=%s window=%s/%s",
                        channel_id,
                        session_id,
                        round_id,
                        pending,
                        stall_windows,
                        TEAM_STALL_CONFIRM_WINDOWS,
                    )
                    continue
                logger.warning(
                    "[TeamStallWatchdog] team round stalled: channel_id=%s session_id=%s "
                    "round_id=%s pending_tasks=%s idle_windows=%s",
                    channel_id,
                    session_id,
                    round_id,
                    pending,
                    stall_windows,
                )
                await broadcast(
                    channel_id,
                    session_id,
                    {
                        "event_type": "chat.processing_status",
                        "session_id": session_id,
                        "rid": round_id,
                        "is_processing": False,
                        "is_complete": True,
                        "error": (
                            f"团队任务停摆：{pending} 个任务已下发但无成员执行，"
                            "请追问推动或重新发起"
                        ),
                    },
                )
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug(
                    "[TeamStallWatchdog] team stall watchdog failed: session_id=%s",
                    session_id,
                    exc_info=True,
                )
                return

    return asyncio.create_task(_watchdog(), name=f"team-stall-watchdog-{session_id}")
