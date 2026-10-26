# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""周期内存回收:主动 gc.collect() + glibc malloc_trim 还页。

背景(ns wmq 2026-10-14 压测定位):会话 adapter 逐出后其对象图以**循环引用**
死亡,refcount 无法回收;而 Python gc 的代际阈值(700,10,10)按"净存活分配数"
推进——短命非循环对象即时 refcount 释放、不推计数,循环垃圾要攒够 ~70k 净存活
才触发一次 gen2 全量收集。高并发会话轮换下,两次 gen2 之间堆积大量待收循环图
(实测 4 分钟窗口 +567 套 agent 图 ≈ 数百 MB),RSS 高水位只升不降。

修复:后台线程按间隔执行 gc.collect()(顺带 malloc_trim(0) 让 glibc 归还空闲
arena 页)。gc.collect() 在 1.8M 对象堆上实测 ~1-4s,放在独立线程不阻塞事件
循环;默认关闭(JIUWENSWARM_GC_RECLAIM_INTERVAL=0),部署侧显式开启。
"""

from __future__ import annotations

import ctypes
import gc
import logging
import os
import threading
import time

logger = logging.getLogger(__name__)


def _rss_kb() -> int:
    try:
        with open("/proc/self/status", encoding="ascii") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except OSError:
        pass
    return -1


def _malloc_trim() -> bool:
    try:
        libc = ctypes.CDLL("libc.so.6")
        return bool(libc.malloc_trim(0))
    except (OSError, AttributeError):
        return False


def _reclaim_once() -> None:
    rss_before = _rss_kb()
    t0 = time.monotonic()
    collected = gc.collect()
    duration_ms = (time.monotonic() - t0) * 1000
    trimmed = _malloc_trim()
    rss_after = _rss_kb()
    logger.info(
        "[mem-reclaimer] gc.collect freed=%d objs duration_ms=%.0f "
        "malloc_trim=%s rss %d->%d kB",
        collected, duration_ms, trimmed, rss_before, rss_after,
    )


def _loop(interval: int) -> None:
    while True:
        time.sleep(interval)
        try:
            _reclaim_once()
        except Exception:  # noqa: BLE001 - 回收失败不终止线程
            logger.warning("[mem-reclaimer] reclaim failed", exc_info=True)


def setup() -> None:
    raw = os.getenv("JIUWENSWARM_GC_RECLAIM_INTERVAL", "")
    interval = int(raw) if raw.strip().isdigit() else 0
    if interval <= 0:
        return
    threading.Thread(
        target=_loop, args=(interval,), daemon=True, name="mem-reclaimer"
    ).start()
    logger.info(
        "[mem-reclaimer] enabled: interval=%ss gc_thresholds=%s",
        interval, gc.get_threshold(),
    )
