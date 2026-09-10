# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""事件循环停摆探针与主线程栈采样器"""

from __future__ import annotations

import asyncio
import logging
import os
import queue
import sys
import threading
import time
import traceback
from collections import deque
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: 模块总开关（env 可关闭）
_ENABLED = os.environ.get(
    "JIUWENSWARM_EVENT_LOOP_MONITOR", "1"
) not in ("0", "false", "False")
#: 心跳间隔（秒）
_HEARTBEAT_INTERVAL_S = 1.0
#: 心跳间隔超过该阈值视为一次停摆（秒）
_STALL_THRESHOLD_S = max(
    1.0, float(os.environ.get("JIUWENSWARM_LOOP_STALL_SECONDS", "2.0"))
)
#: 两次停摆告警之间的最小间隔（秒）。防止"反复短停摆"把 warning 打爆；
#: 保证任意工况下告警 ≤ 60/_MIN_REPORT_INTERVAL_S 条/分钟。
_MIN_REPORT_INTERVAL_S = float(
    os.environ.get("JIUWENSWARM_LOOP_REPORT_MIN_INTERVAL", "10.0")
)
#: 主线程栈采样周期（秒）
_SAMPLE_INTERVAL_S = 0.5
#: 环形缓冲容量（条），0.5s/条 → 60s 可回放窗口
_RING_SIZE = 120
#: 单条栈签名保留的顶层帧数
_SIG_TOP_FRAMES = 6
#: 停摆结束后补采全栈的次数与间隔
_BOOST_SAMPLES = 3
_BOOST_INTERVAL_S = 0.25
#: 回放窗口在停摆区间之外再放宽的秒数（吸收采样相位误差）
_REPLAY_SLOP_S = 1.0
#: 内存趋势日志间隔（秒）。2 分钟粒度足够看出 OOM 走势，又不会刷屏
_MEM_TREND_INTERVAL_S = float(
    os.environ.get("JIUWENSWARM_MEM_TREND_INTERVAL", "120.0")
)
#: 进程 RSS 占系统内存比例告警阈值（0~1）。这是 OOM 前兆最直接的信号；
#: 连续上升不作告警条件——用户连续建任务时内存同样会连续上升，会误报。
_MEM_WARN_PCT = float(
    os.environ.get("JIUWENSWARM_MEM_WARN_PCT", "0.85")
)

#: 进程内只挂载一次的标志
_installed = False
#: 心跳协程引用，防止被 GC 回收
_heartbeat_task: Optional[asyncio.Task] = None
#: 内存趋势协程引用，防止被 GC 回收
_mem_trend_task: Optional[asyncio.Task] = None
#: 采样线程句柄（幂等 start / stop 用）
_sampler_thread: Optional[threading.Thread] = None
#: 采样线程停止信号（stop_event_loop_monitor 置位，线程下一拍退出）
_stop_sampler = threading.Event()
#: 线程安全的停摆事件队列（元素为 (gap, 停摆结束时刻)）
_events: "queue.SimpleQueue[tuple[float, float]]" = queue.SimpleQueue()
#: 环形缓冲：（monotonic 时刻, 主线程栈签名）
_ring: deque[tuple[float, str]] = deque(maxlen=_RING_SIZE)


def _frame_signature(frame: Any) -> str:
    """把一条调用栈压成 ``file:line:func <- ...`` 的紧凑签名（存缓冲用）。"""
    parts: list[str] = []
    total = 0
    f = frame
    while f is not None and total < 100:
        code = f.f_code
        short = code.co_filename.replace("\\", "/").rsplit("/", 1)[-1]
        if total < _SIG_TOP_FRAMES:
            parts.append(f"{short}:{f.f_lineno}:{code.co_name}")
        total += 1
        f = f.f_back
    return " <- ".join(parts) + f" (total_frames={total})"


def _all_thread_frames() -> dict[int, Any]:
    """返回所有线程的当前栈帧字典（键为线程标识）。"""
    # sys._current_frames 是私有 API，用 getattr 规避静态检查
    return getattr(sys, "_current_frames")()


def _format_full_stack(frame: Any) -> str:
    """格式化整条调用栈（补采全栈用）。"""
    return "".join(traceback.format_stack(frame)).strip()


def _mem_stats() -> tuple[float, float]:
    """返回 (进程RSS MB, 系统总内存 MB)。

    psutil 优先（项目已有依赖）；psutil 不可用或运行时受限（如容器/沙箱
    权限不足）时回退读 /proc。失败返回 (0, 0)。
    """
    try:
        try:
            import psutil

            proc = psutil.Process()
            rss_mb = proc.memory_info().rss / (1024 * 1024)
            total_mb = psutil.virtual_memory().total / (1024 * 1024)
            return rss_mb, total_mb
        except Exception:
            # psutil 不可用或运行时受限（容器/沙箱里 AccessDenied、
            # NoSuchProcess、virtual_memory() 失败等），一律回退 /proc 路径
            info: dict[str, int] = {}
            with open("/proc/meminfo", encoding="utf-8") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2:
                        info[parts[0].rstrip(":")] = int(parts[1])  # kB
            total_mb = info.get("MemTotal", 0) / 1024
            rss_mb = 0.0
            with open("/proc/self/status", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        rss_mb = int(line.split()[1]) / 1024  # kB → MB
                        break
            return rss_mb, total_mb
    except Exception:
        return 0.0, 0.0


async def memory_trend_loop() -> None:
    """内存趋势日志：每 ``_MEM_TREND_INTERVAL_S`` 打一条 RSS 走势。

    正常时 info 一条，delta 供人工判断趋势（只涨不落多半是异常）；
    占比超过 ``_MEM_WARN_PCT`` 时升级 warning（OOM 前兆）。
    进程被 OOM-kill 时本日志永久停更，正好作为死后时间戳佐证。
    """
    last_rss = 0.0
    while True:
        await asyncio.sleep(_MEM_TREND_INTERVAL_S)
        rss_mb, total_mb = _mem_stats()
        if total_mb <= 0:
            logger.warning("[MemTrend] 内存信息读取失败（已忽略本轮）")
            continue
        pct = rss_mb / total_mb
        delta_mb = rss_mb - last_rss if last_rss else 0.0
        last_rss = rss_mb
        if pct >= _MEM_WARN_PCT:
            logger.warning(
                "[MemTrend] rss=%.0fMB total=%.0fMB pct=%.1f%% delta=%.0fMB（占比超阈值，疑似OOM前兆）",
                rss_mb,
                total_mb,
                pct * 100,
                delta_mb,
                extra={
                    "mem_rss_mb": round(rss_mb, 1),
                    "mem_total_mb": round(total_mb, 1),
                    "mem_pct": round(pct * 100, 1),
                },
            )
        else:
            logger.info(
                "[MemTrend] rss=%.0fMB total=%.0fMB pct=%.1f%% delta=%.0fMB",
                rss_mb,
                total_mb,
                pct * 100,
                delta_mb,
                extra={
                    "mem_rss_mb": round(rss_mb, 1),
                    "mem_total_mb": round(total_mb, 1),
                },
            )


async def loop_heartbeat_loop() -> None:
    """事件循环心跳：差分测停摆，阻塞结束后第一拍即告警并通知采样器。"""
    last = time.monotonic()
    last_report = 0.0
    while True:
        await asyncio.sleep(_HEARTBEAT_INTERVAL_S)
        now = time.monotonic()
        gap = now - last
        last = now
        if gap < _STALL_THRESHOLD_S:
            continue
        # 限流：同一次停摆只报告一次（阻塞期间可能欠睡多拍，恢复后全挤在一起），
        # 且相邻两次报告至少间隔 _MIN_REPORT_INTERVAL_S，反复短停摆也打不满。
        if now - last_report < _MIN_REPORT_INTERVAL_S:
            last = now  # 记录欠账，避免恢复后再来一记"伪停摆"
            continue
        last_report = now
        logger.warning(
            "[LoopMonitor] 事件循环停摆 ~%.1fs（结束于 %s）",
            gap,
            time.strftime("%H:%M:%S"),
            extra={"loop_stall_seconds": round(gap, 1)},
        )
        _events.put((gap, now))


class StackSampler:
    """采样线程：周期性缓存主线程栈签名，停摆时回放并补采全栈。"""

    @classmethod
    def start(cls) -> None:
        global _sampler_thread
        if not _ENABLED:
            return
        # 正常在运行时 ensure() 会直接返回、不会走到这里；若残留旧线程
        # （如 stop 尚未跑完的边界场景）则等它退出后重建，避免双线程
        if _sampler_thread is not None:
            if _sampler_thread.is_alive():
                _sampler_thread.join(timeout=_SAMPLE_INTERVAL_S + 0.5)
            _sampler_thread = None
        _stop_sampler.clear()
        _sampler_thread = threading.Thread(
            target=cls._run,
            name="agent-server-loop-stack-sampler",
            daemon=True,
        )
        _sampler_thread.start()

    @classmethod
    def _main_thread_id(cls) -> Optional[int]:
        # 3.12 起 main_thread().ident 在启动早期可能为 None，多取几次
        for _ in range(5):
            ident = threading.main_thread().ident
            if ident is not None:
                return ident
            time.sleep(0.1)
        return None

    @classmethod
    def _sample(cls) -> tuple[float, Optional[str]]:
        fs = _all_thread_frames()
        tid = cls._main_thread_id()
        frame = fs.get(tid) if tid else None
        if frame is None:
            return time.monotonic(), None
        return time.monotonic(), _frame_signature(frame)

    @classmethod
    def _emit(cls, gap: float, end_ts: float) -> None:
        """打停摆窗口证据 + 补采全栈。"""
        start_ts = end_ts - gap - _REPLAY_SLOP_S
        hits = [
            (ts, sig) for ts, sig in _ring if start_ts <= ts <= end_ts
        ][-3:]
        if hits:
            logger.warning(
                "[LoopMonitor] 停摆窗口（结束前 %s）主线程栈签名:\n%s",
                "、".join(f"{end_ts - ts:.1f}s" for ts, _ in hits),
                "\n".join(
                    f"    [{end_ts - ts:.1f}s前] {sig}" for ts, sig in hits
                ),
            )
        # 补采：阻塞已结束，抓到的多是恢复后的栈；去重后只输出**最近一条**
        # 供对照。阻塞现场以停摆窗口内（_ring 回放）的采样为准。
        seen: list[str] = []
        for _ in range(_BOOST_SAMPLES):
            time.sleep(_BOOST_INTERVAL_S)
            fs = _all_thread_frames()
            tid = cls._main_thread_id()
            frame = fs.get(tid) if tid else None
            if frame is not None:
                full = _format_full_stack(frame)
                if full not in seen:
                    seen.append(full)
        if seen:
            logger.warning(
                "[LoopMonitor] 停摆附近主线程全栈（最近一条）:\n%s", seen[-1]
            )

    @classmethod
    def _run(cls) -> None:
        # 每次醒来先检查停止信号；stop_event_loop_monitor() 置位后下一拍退出
        while not _stop_sampler.is_set():
            time.sleep(_SAMPLE_INTERVAL_S)
            try:
                ts, sig = cls._sample()
                if sig is not None:
                    _ring.append((ts, sig))
                while True:
                    try:
                        gap, end_ts = _events.get_nowait()
                    except queue.Empty:
                        break
                    cls._emit(gap, end_ts)
            except Exception:
                # 探针绝不能拖垮主流程
                logger.exception("[LoopMonitor] 采样器异常（已忽略）")


async def ensure_event_loop_monitor() -> None:
    """幂等挂载事件循环停摆探针（采样线程 + 心跳协程 + 内存趋势协程）。

    - 多次调用安全：已在运行则直接返回；
    - ``_installed`` 在全部操作成功后才置位：任一步失败会清理已创建的资源并
      重新抛出，由调用方记录日志，**下次调用可重试**，不会永久失效；
    - 事件循环重启/测试场景请先调用 :func:`stop_event_loop_monitor` 重置。
    """
    global _installed, _heartbeat_task, _mem_trend_task
    if not _ENABLED or _installed:
        return
    try:
        StackSampler.start()
        _heartbeat_task = asyncio.create_task(
            loop_heartbeat_loop(), name="event-loop-stall-heartbeat"
        )
        _mem_trend_task = asyncio.create_task(
            memory_trend_loop(), name="memory-trend-log"
        )
    except Exception:
        # 部分资源可能已创建：清理后重抛（_installed 保持 False，可重试）
        for task in (_heartbeat_task, _mem_trend_task):
            if task is not None and not task.done():
                task.cancel()
        _heartbeat_task = None
        _mem_trend_task = None
        raise
    _installed = True
    logger.info(
        "[LoopMonitor] 停摆探针已挂载: 阈值=%.1fs 采样=%.1fs",
        _STALL_THRESHOLD_S,
        _SAMPLE_INTERVAL_S,
    )


def stop_event_loop_monitor() -> None:
    """停止事件循环监控（供测试与事件循环重启场景使用）。

    取消两个协程、置位停止信号并等待采样线程退出，然后重置全部状态；
    之后可再次调用 :func:`ensure_event_loop_monitor` 重新挂载。
    本函数最多阻塞约一个采样周期（0.5s）用于等待线程退出。
    """
    global _installed, _heartbeat_task, _mem_trend_task
    global _sampler_thread, _events, _ring
    _stop_sampler.set()
    for task in (_heartbeat_task, _mem_trend_task):
        if task is not None and not task.done():
            task.cancel()
    _heartbeat_task = None
    _mem_trend_task = None
    # 同步等待采样线程退出，避免"再次挂载前 clear 停止信号"造成旧线程残留
    if _sampler_thread is not None and _sampler_thread.is_alive():
        _sampler_thread.join(timeout=_SAMPLE_INTERVAL_S + 0.5)
    _sampler_thread = None
    # 清空遗留的停摆事件与历史采样，保证重新挂载时从干净状态开始
    _events = queue.SimpleQueue()
    _ring = deque(maxlen=_RING_SIZE)
    _installed = False