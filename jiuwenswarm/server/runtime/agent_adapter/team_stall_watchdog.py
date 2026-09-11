# coding: utf-8
"""团队回合停摆看门狗。

死亡探针（team_helpers._schedule_leader_round_death_probe）只管"leader 模型
错误后零产出"；停摆是另一族：leader 正常收尾（或重试断片后自认无事可做）、
任务已下发但零成员在途——回合无人推进，team.completed 因任务非终态永不成立，
流靠 keepalive 吊命，前端永远"正在思考"。

判定（每个窗口）：流存活 + 无任何流帧活动 + 零在途成员 + 有未终态任务
+ 窗口内无任务流转（updated_at 判活）+ DB 快照对照同样零在途，
连续 _TEAM_STALL_CONFIRM_WINDOWS 个窗口成立 → 广播 processing_status 失败终态。

背景：
1. 在途口径含 starting/restarting——成员进程拉起/重启耗时（模型客户端初始化、
   MCP 服务器、上下文恢复）可超 90s，此前只认 busy，拉起期被误判"零在途"；
2. live 快照判零在途时再读 DB 快照对照——DAO 直写点不经事件，live 内存视图
   可能滞后，DB 是落库真值；对照不可用退回 live-only 判定（不因此赦免真停摆）；
3. 任务 updated_at 在窗口内有更新 = 刚发生认领/推进，直接证有活动；
4. leader kernel 运行时活性——状态采样有两个结构性盲区：agentic 循环
   "失败透传→下一迭代"的间隙期成员/leader 的 status 采样就是 ready；且快照 members
   剔除 leader，leader 长考/重试对采样不可见。kernel lifecycle_state
   == 'running' 是"回合仍在推进"的运行时真值，覆盖这两个盲区。

退化方向与死亡探针一致：快照不可用/有任何活动迹象都按"不停摆"处理。
跨轮存活：收尾信号变化不终止看门狗，只重置计数（长寿命流多轮复用）。

broadcast 由调用方注入（team_helpers._broadcast_event），避免对本模块的
循环依赖（team_helpers 是本模块的调用方）。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

from openjiuwen.core.common.logging import logger

from jiuwenswarm.agents.harness.team import get_team_manager

# 停摆判定窗口与确认次数：连续两窗口成立才判死——任务下发到成员启动有正常
# 延迟，单窗口误杀风险高。窗口需大于成员启动的典型耗时。
TEAM_STALL_WATCHDOG_SEC = 45.0
TEAM_STALL_CONFIRM_WINDOWS = 2

# 任务流转判活的回溯视野（秒）：任务 updated_at 落在此视野内 = 窗口内有认领/
# 推进活动。独立于判定窗口常量——测试会压缩判定窗口，判活视野不能跟着缩。
TASK_ACTIVITY_HORIZON_SEC = 45.0

# 任务终态集合：不在其中的都视为"待推进"
TASK_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})

# 判定"成员仍在工作"的生命周期状态：busy=干活中；starting/restarting=进程
# 拉起/重启途中（拉起链——模型客户端初始化/MCP 服务器/上下文恢复——可超
# 90s，只认 busy 会在拉起期误杀）。
# 与 team_helpers 死亡探针同口径。
MEMBER_IN_FLIGHT_STATUSES = frozenset({"busy", "starting", "restarting"})

# 判定"成员仍在工作"的执行中状态（生命周期之外的旁证）
MEMBER_IN_FLIGHT_EXEC_STATUSES = frozenset({"starting", "running", "completing"})

BroadcastFn = Callable[[str | None, str, dict[str, Any]], Awaitable[None]]


def _member_is_in_flight(member: dict[str, Any]) -> bool:
    """成员是否在途：生命周期在忙/拉起/重启，或执行细态进行中。"""
    return (
            member.get("status") in MEMBER_IN_FLIGHT_STATUSES
            or member.get("execution_status") in MEMBER_IN_FLIGHT_EXEC_STATUSES
    )


def _task_is_recently_active(task: dict[str, Any], horizon_ms: float) -> bool:
    """任务在 horizon 内有状态流转（updated_at 为 epoch 毫秒，见 agent-core
    engine.get_current_time）。时间戳缺失/不可解析按不活跃处理（保守不误伤）。"""
    raw = task.get("updated_at")
    try:
        updated_ms = float(raw)
    except (TypeError, ValueError):
        return False
    if updated_ms <= 0:
        return False
    return (time.time() * 1000 - updated_ms) < horizon_ms


def _read_progress(snapshot: dict[str, Any] | None) -> tuple[int, int, bool] | None:
    """从任一来源的快照读 (在途成员数, 未终态任务数, 窗口内有任务流转)。"""
    if not snapshot:
        return None
    in_flight = sum(1 for m in snapshot.get("members", []) if _member_is_in_flight(m))
    pending = 0
    recent = False
    horizon_ms = TASK_ACTIVITY_HORIZON_SEC * 1000
    for t in snapshot.get("tasks", []):
        if str(t.get("status") or "") in TASK_TERMINAL_STATUSES:
            continue
        pending += 1
        if _task_is_recently_active(t, horizon_ms):
            recent = True
    return in_flight, pending, recent


async def team_progress_snapshot(
        channel_id: str | None, session_id: str
) -> tuple[int, int, bool] | None:
    """live 快照（monitor 内存视图）；不可用返回 None（调用方按"不停摆"退化）。

    快照源与死亡探针相同（team_monitor_handler.get_team_snapshot；members 已
    剔除 leader）；停摆判定需要同时看成员与任务，一次快照取齐。
    """
    try:
        tm = get_team_manager(channel_id)
        monitor_handler = tm.get_monitor_handler(session_id)
        if monitor_handler is None:
            return None
        return _read_progress(await monitor_handler.get_team_snapshot())
    except Exception:
        logger.debug(
            "[TeamStallWatchdog] team progress snapshot failed: session_id=%s",
            session_id,
            exc_info=True,
        )
        return None


async def team_progress_snapshot_db(
        channel_id: str | None, session_id: str
) -> tuple[int, int, bool] | None:
    """DB 快照对照（team.db 落库真值）：live 判零在途时的第二读数。

    DAO 直写点（recovery/restart 等路径）不经事件，live 内存视图可能滞后于
    DB；两队任一显示在途都按"不停摆"。team_name 解析链与
    agent_ws_server._handle_team_snapshot 一致；解析不到/读取失败返回 None。
    """
    try:
        from jiuwenswarm.agents.harness.team.handlers.team_monitor_handler import (
            TeamMonitorHandler,
        )
        from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata

        tm = get_team_manager(channel_id)
        team_name = str(tm.get_active_team_name(session_id) or "").strip()
        if not team_name:
            team_name = str((get_session_metadata(session_id) or {}).get("team_name") or "").strip()
        if not team_name:
            return None
        return _read_progress(
            await TeamMonitorHandler.get_team_snapshot_from_db(session_id, team_name)
        )
    except Exception:
        logger.debug(
            "[TeamStallWatchdog] team progress snapshot (db) failed: session_id=%s",
            session_id,
            exc_info=True,
        )
        return None


async def team_runtime_liveness(channel_id: str | None, session_id: str) -> bool | None:
    """leader kernel 协调循环活性：True = 回合仍在推进（含模型长考与 agentic
    迭代间隙——这两类场景成员/leader 的 DB 状态采样都是 ready，快照判定必误判）。

    访问链：GLOBAL_RUNNER → _team_runtime_manager → pool → TeamAgent
    → _coordination._lifecycle_state。pool 无条目/kernel 不可达（团队未启动、
    已拆除、分布式模式 leader 不在本进程）返回 None——信息缺失，调用方退回
    既有快照判定（不因此赦免、也不因此击杀）；kernel 存在但非 'running'
    （paused/stopped/idle）返回 False——leader 确实停了，是有效停摆证据。
    """
    try:
        from openjiuwen.core.runner.runner import GLOBAL_RUNNER

        from jiuwenswarm.agents.harness.team.team_manager import (
            _runner_team_runtime_manager,
        )
        from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata

        tm = get_team_manager(channel_id)
        team_name = str(tm.get_active_team_name(session_id) or "").strip()
        if not team_name:
            team_name = str((get_session_metadata(session_id) or {}).get("team_name") or "").strip()
        if not team_name:
            return None
        runtime_mgr = _runner_team_runtime_manager(GLOBAL_RUNNER)
        pool = getattr(runtime_mgr, "pool", None)
        get_active = getattr(pool, "get", None)
        if not callable(get_active):
            return None
        active = await get_active(team_name)
        coordination = getattr(getattr(active, "agent", None), "_coordination", None)
        state = getattr(coordination, "_lifecycle_state", None)
        if not isinstance(state, str) or not state:
            return None
        return state == "running"
    except Exception:
        logger.debug(
            "[TeamStallWatchdog] team runtime liveness failed: session_id=%s",
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
                in_flight, pending, recent = progress
                if in_flight > 0 or pending == 0 or recent:
                    stall_windows = 0
                    continue
                # leader kernel 活性：agentic 迭代间隙/leader 长考期间成员与
                # leader 的状态采样都是 ready，快照判定必误判——看运行时真值。
                # None=不可达（信息缺失，继续既有判定）；False=leader 确实停了
                alive = await team_runtime_liveness(channel_id, session_id)
                if alive:
                    stall_windows = 0
                    continue
                # live 判零在途：DB 对照复核（live 内存视图滞后于 DAO 直写的
                # 盲区兜底）；对照不可用退回 live-only 判定（保留击杀能力——
                # 误杀主场景已由在途口径扩展+任务判活覆盖，不依赖对照）
                db_progress = await team_progress_snapshot_db(channel_id, session_id)
                if db_progress is not None:
                    db_in_flight, db_pending, db_recent = db_progress
                    if db_in_flight > 0 or db_recent:
                        stall_windows = 0
                        continue
                    pending = max(pending, db_pending)
                if pending == 0:
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
