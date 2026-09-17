# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Live async-state dump for diagnosing coroutine stalls and deadlocks.

Service entrypoints call :func:`install_async_dump_handler` once; afterwards
``kill -USR1 <pid>`` snapshots the process without stopping it: every thread
stack, every pending asyncio task (repr, awaited future, coroutine stack) and
every asyncio primitive/queue that currently has waiters. Suspended coroutines
live on no thread stack, so thread-level tools (py-spy, faulthandler) cannot
see them; this dump is the coroutine-level complement.

SIGUSR1 does not exist on Windows, so installation is a no-op there.
:func:`dump_async_state` remains directly callable on all platforms.
"""

from __future__ import annotations

import asyncio
import gc
import io
import logging
import os
import signal
import sys
import threading
import time
import traceback
from pathlib import Path
from types import FrameType
from typing import TextIO

from jiuwenswarm.common.utils import get_logs_dir

logger = logging.getLogger(__name__)

_SYNC_PRIMITIVE_TYPES = (asyncio.Lock, asyncio.Event, asyncio.Condition, asyncio.Semaphore)


def _write_thread_stacks(out: TextIO) -> None:
    out.write("\n########## THREAD STACKS ##########\n")
    thread_names = {t.ident: t.name for t in threading.enumerate()}
    for thread_id, frame in sys._current_frames().items():
        out.write(f"\n--- Thread {thread_id} ({thread_names.get(thread_id, '?')}) ---\n")
        out.write("".join(traceback.format_stack(frame)))


def _collect_async_objects() -> tuple[list[asyncio.Task], list[object], list[asyncio.Queue]]:
    """Scan the gc heap once for tasks, sync primitives and queues.

    ``asyncio.all_tasks()`` needs a running loop in the calling thread and
    misses loops owned by other threads, so a full gc scan is used instead.
    """
    tasks: list[asyncio.Task] = []
    primitives: list[object] = []
    queues: list[asyncio.Queue] = []
    for obj in gc.get_objects():
        try:
            if isinstance(obj, asyncio.Task):
                tasks.append(obj)
            elif isinstance(obj, _SYNC_PRIMITIVE_TYPES):
                primitives.append(obj)
            elif isinstance(obj, asyncio.Queue):
                queues.append(obj)
        except Exception:
            continue
    return tasks, primitives, queues


def _write_tasks(out: TextIO, tasks: list[asyncio.Task]) -> None:
    pending = [t for t in tasks if not t.done()]
    out.write("\n########## ASYNCIO TASKS ##########\n")
    out.write(f"total={len(tasks)} pending={len(pending)}\n")
    for index, task in enumerate(pending):
        out.write(f"\n===== Task #{index} =====\n")
        try:
            out.write(f"repr: {task!r}\n")
            out.write(f"waiting on: {getattr(task, '_fut_waiter', None)!r}\n")
            stack_buf = io.StringIO()
            task.print_stack(file=stack_buf)
            out.write(stack_buf.getvalue())
        except Exception as exc:
            out.write(f"<error dumping task: {exc!r}>\n")


def _write_waiting_primitives(out: TextIO, primitives: list[object], queues: list[asyncio.Queue]) -> None:
    out.write("\n########## SYNC PRIMITIVES WITH WAITERS ##########\n")
    waiting_count = 0
    for primitive in primitives:
        try:
            waiters = getattr(primitive, "_waiters", None)
            if waiters:
                waiting_count += 1
                out.write(f"{primitive!r}  waiters={len(waiters)}\n")
        except Exception:
            continue
    for queue in queues:
        try:
            getters = getattr(queue, "_getters", ())
            putters = getattr(queue, "_putters", ())
            if getters or putters:
                waiting_count += 1
                out.write(f"{queue!r}  getters={len(getters)} putters={len(putters)}\n")
        except Exception:
            continue
    out.write(f"primitives_with_waiters={waiting_count}\n")


def dump_async_state(service_name: str) -> Path | None:
    """Write a full thread/coroutine snapshot to the dump directory.

    Safe to call from a signal handler: it only reads interpreter state and
    never raises. The gc heap scan can pause the process for a few seconds,
    which is acceptable while diagnosing a stall.

    Args:
        service_name: Short service identifier used in the dump file name.

    Returns:
        Path of the written dump file, or None if the dump failed.
    """
    try:
        dump_dir = get_logs_dir() / "async_dump"
        dump_dir.mkdir(parents=True, exist_ok=True)
        now = time.time()
        millis = int(now * 1000) % 1000
        timestamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(now)) + f"_{millis:03d}"
        dump_path = dump_dir / f"{service_name}_{os.getpid()}_{timestamp}.txt"
        with open(dump_path, "w", encoding="utf-8") as out:
            out.write(f"===== ASYNC STATE DUMP service={service_name} pid={os.getpid()} time={timestamp} =====\n")
            out.write(f"argv: {sys.argv!r}\n")
            _write_thread_stacks(out)
            tasks, primitives, queues = _collect_async_objects()
            _write_tasks(out, tasks)
            _write_waiting_primitives(out, primitives, queues)
            out.write("\n===== END OF DUMP =====\n")
        logger.info("[debug_dump] async state dumped to %s", dump_path)
        return dump_path
    except Exception:
        logger.exception("[debug_dump] async state dump failed")
        return None


def install_async_dump_handler(service_name: str) -> None:
    """Register a SIGUSR1 handler that snapshots live async state to a file.

    Must be called from the main thread, before the event loop starts. On
    platforms without SIGUSR1 (Windows) this is a no-op.

    Args:
        service_name: Short service identifier used in dump file names.
    """
    if not hasattr(signal, "SIGUSR1"):
        logger.debug("[debug_dump] SIGUSR1 unavailable; async dump handler not installed")
        return

    def _handler(_signum: int, _frame: FrameType | None) -> None:
        dump_async_state(service_name)

    signal.signal(signal.SIGUSR1, _handler)
    logger.info(
        "[debug_dump] async dump handler installed for %s: kill -USR1 %s",
        service_name,
        os.getpid(),
    )
