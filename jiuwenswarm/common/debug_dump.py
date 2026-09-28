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
import atexit
import contextlib
import faulthandler
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
from types import FrameType, TracebackType
from typing import TextIO

from jiuwenswarm.common.utils import get_logs_dir

logger = logging.getLogger(__name__)

# debug_dump 的日志需归属到调用进程的日志分桶（按 utils 的 logger 名前缀分类），
# 否则 jiuwenswarm.common.* 一律落入 gateway 桶：非 gateway 进程（如 jiuwenswarm-web）
# 会在默认日志目录写出 gateway.log，与"gateway 日志只在 AGENTOS_GATEWAY_LOG_DIR"的
# 部署约定冲突。未知 service_name 回退模块 logger（gateway 桶，保持旧行为）。
_SERVICE_LOGGER_NAMES = {
    "web": "jiuwenswarm.channels.web.debug_dump",
    "agentserver": "jiuwenswarm.server.debug_dump",
    "gateway": "jiuwenswarm.gateway.debug_dump",
}


def _service_logger(service_name: str) -> logging.Logger:
    """Return a logger whose records classify into the calling process's bucket."""
    return logging.getLogger(_SERVICE_LOGGER_NAMES.get(service_name, __name__))

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
    log = _service_logger(service_name)
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
        log.info("[debug_dump] async state dumped to %s", dump_path)
        return dump_path
    except Exception:
        log.exception("[debug_dump] async state dump failed")
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
    _service_logger(service_name).info(
        "[debug_dump] async dump handler installed for %s: kill -USR1 %s",
        service_name,
        os.getpid(),
    )


def _crash_exit(code: int) -> None:
    os._exit(code)  # pylint: disable=protected-access  # public API; underscore is historical


# faulthandler dump targets: raw OS file descriptors, allocated with os.open
# and released with os.close (atexit / reinstall). They must stay valid for
# the process lifetime - the handler writes to them from the crash itself -
# and faulthandler.enable() accepts a bare fd.
_CRASH_LOG_FDS: dict[str, int] = {}


def _release_crash_fd(fd: int) -> None:
    with contextlib.suppress(Exception):
        faulthandler.disable()
    with contextlib.suppress(OSError):
        os.close(fd)


def _enable_faulthandler(service_name: str) -> None:
    """Dump native-crash thread stacks to a durable file instead of stderr.

    stderr of a service-form Windows run is usually lost, which is exactly
    when the dump is needed most. Falls back to stderr when the logs dir is
    unavailable.
    """
    previous = _CRASH_LOG_FDS.pop(service_name, None)
    if previous is not None:
        _release_crash_fd(previous)
    try:
        path = get_logs_dir() / f"faulthandler-{service_name}.log"
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    except OSError:
        faulthandler.enable()
        return
    try:
        faulthandler.enable(file=fd, all_threads=True)
    except (OSError, ValueError):
        with contextlib.suppress(OSError):
            os.close(fd)
        faulthandler.enable()
        return
    _CRASH_LOG_FDS[service_name] = fd
    atexit.register(_release_crash_fd, fd)


def install_crash_exit_handler(service_name: str) -> None:
    """Make crashes loud: leave a stack behind and exit with CRASH_EXIT_CODE.

    Complements install_async_dump_handler. An unhandled exception used to end
    the process with exit code 1, which the supervisor cannot tell from
    ``taskkill /F`` on Windows, so it read as an intentional stop and tore the
    whole service down without respawn. The custom excepthook logs the full
    stack through the service's logging (persisted even when the console is
    not captured) and exits with the dedicated CRASH_EXIT_CODE instead.
    faulthandler covers native crashes (access violations, aborts) where no
    Python hook runs any more: it dumps every thread stack to a file under
    the logs dir, and the resulting NTSTATUS/signal exit code is already
    classified as a crash by the supervisor.

    Must be called from the main thread, before the event loop starts.
    """
    from jiuwenswarm.common.process_supervision import CRASH_EXIT_CODE

    log = _service_logger(service_name)

    _enable_faulthandler(service_name)

    def _excepthook(exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None) -> None:
        formatted = "".join(traceback.format_exception(exc_type, exc, tb))
        with contextlib.suppress(Exception):
            log.critical(
                "[crash] unhandled exception in %s, exiting with code %d:\n%s",
                service_name,
                CRASH_EXIT_CODE,
                formatted,
            )
            for handler in (*logging.getLogger().handlers, *log.handlers):
                handler.flush()
        _crash_exit(CRASH_EXIT_CODE)

    sys.excepthook = _excepthook
    log.info("[debug_dump] crash exit handler installed for %s (exit code %d)", service_name, CRASH_EXIT_CODE)
