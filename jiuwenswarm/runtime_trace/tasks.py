# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Helpers for bounded periodic-task spans.

A task span is only materialised after the work finished: fast, successful
runs produce nothing, so the common path costs two clock reads.
"""

from __future__ import annotations

from jiuwenswarm.runtime_trace import state as trace_state


def record_task(
    name: str,
    start_mono_ns: int,
    end_mono_ns: int,
    *,
    failed: bool = False,
) -> None:
    """Record one completed periodic task when it was slow or failed."""
    try:
        settings = trace_state.current_settings()
        if settings is None or not getattr(settings, "enabled", False):
            return
        elapsed_ms = (end_mono_ns - start_mono_ns) / 1_000_000.0
        if not failed and elapsed_ms <= settings.slow_task_ms:
            return
        recorder = trace_state.current_recorder()
        if recorder is None:
            return
        handle = recorder.start(name, level="normal", mono_start=start_mono_ns)
        recorder.end(
            handle,
            "error" if failed else "slow",
            mono_end=end_mono_ns,
        )
    except Exception:  # noqa: BLE001 - task tracing never changes scheduler flow
        return
