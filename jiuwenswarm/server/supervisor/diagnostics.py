# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bounded thread/task snapshots without copying queued business payloads."""

from __future__ import annotations

import asyncio
import faulthandler
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

_MAX_DUMPS = 12


def process_resources(pid: int | None) -> dict[str, Any]:
    """Collect bounded OS counters without reading command lines or payloads."""
    import psutil

    try:
        memory = psutil.virtual_memory()
    except (OSError, psutil.Error) as exc:
        return {"resources_unavailable": type(exc).__name__}
    result: dict[str, Any] = {"system_memory_available": memory.available}
    if pid is None:
        return result
    try:
        process = psutil.Process(pid)
        with process.oneshot():
            result.update(rss=process.memory_info().rss, threads=process.num_threads(),
                          status=process.status(), cpu_seconds=sum(process.cpu_times()[:2]))
    except (OSError, psutil.Error) as exc:
        result["process_unavailable"] = type(exc).__name__
    return result


def dump_runtime_state(directory: Path, snapshot: dict[str, Any]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    # Retain bounded incident evidence across replacements, not an unbounded
    # sequence of supervisor dumps during a long unhealthy period.
    existing = sorted(directory.glob("supervisor-*.txt"), key=lambda path: path.stat().st_mtime)
    for old in existing[:-(_MAX_DUMPS - 1)]:
        old.unlink(missing_ok=True)
    target = directory / f"supervisor-{os.getpid()}-{time.time_ns()}.txt"
    thread_names = {thread.ident: thread.name for thread in threading.enumerate()}
    with target.open("w", encoding="utf-8") as out:
        out.write(json.dumps(snapshot, ensure_ascii=False) + "\n")
        out.write("THREAD STACKS\n")
        for ident, name in list(thread_names.items())[:64]:
            out.write(f"Thread {ident} {name}\n")
        # Flush buffered text before the public API writes to the same file
        # descriptor, keeping the JSON header and thread/task sections ordered.
        out.flush()
        out.write("PYTHON STACK TRACE\n")
        faulthandler.dump_traceback(file=out, all_threads=True)
        tasks = sorted(asyncio.all_tasks(), key=lambda task: task.get_name())
        out.write(f"ASYNCIO TASKS total={len(tasks)} limit=64\n")
        for task in tasks[:64]:
            out.write(f"Task {task.get_name()}\n")
            awaitable = task.get_coro()
            for _ in range(16):
                if awaitable is None:
                    break
                frame = getattr(awaitable, "cr_frame", None) or getattr(
                    awaitable, "gi_frame", None
                )
                if frame is not None:
                    out.write(
                        f"  {frame.f_code.co_filename}:{frame.f_lineno} {frame.f_code.co_name}\n"
                    )
                awaitable = getattr(awaitable, "cr_await", None) or getattr(
                    awaitable, "gi_yieldfrom", None
                )
    return target
