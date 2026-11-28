# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Assembly, teardown, and shared accessors for the runtime trace recorder.

Only the enabled path imports this module. Every heavy dependency it needs
(OpenTelemetry, Core, SQLite) is loaded by the modules it assembles.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jiuwenswarm.runtime_trace.clock import ClockBase
from jiuwenswarm.runtime_trace.config import (
    RuntimeTraceSettings,
    _DRAIN_CAP_SECONDS,
    _READY_TIMEOUT_SECONDS,
)
from jiuwenswarm.runtime_trace import state as trace_state
from jiuwenswarm.runtime_trace.consumer import RuntimeRecordSink
from jiuwenswarm.runtime_trace.recorder import (
    RuntimeRecorder,
    SpanKinds,
    build_runtime_tracer,
    resolve_root_context,
)
from jiuwenswarm.runtime_trace.store import RuntimeTraceStore
from jiuwenswarm.runtime_trace.writer import RuntimeTraceWriter

logger = logging.getLogger(__name__)

_SERVICE_NAMES = {
    "front": "jiuwenswarm-agentserver-front",
    "worker": "jiuwenswarm-agentserver-worker",
}


def _service_name_for(configured: str, role: str) -> str:
    """Return the OTel ``service.name`` for *role*.

    A configured base name keeps the role suffix so Front and Worker stay
    distinguishable in a collector that receives both.
    """
    if configured:
        return f"{configured}-{role}"
    return _SERVICE_NAMES.get(role, f"jiuwenswarm-agentserver-{role}")

_EARLY_PHASE_NAMES = {
    "config": "startup.phase.config",
    "ipc_listening": "startup.phase.ipc_listening",
    "preload": "startup.phase.preload",
}

_lock = threading.Lock()


@dataclass
class _Components:
    recorder: RuntimeRecorder
    provider: Any
    processor: Any
    sink: RuntimeRecordSink
    writer: RuntimeTraceWriter
    clock: ClockBase
    settings: RuntimeTraceSettings
    role: str
    run_id: str


_components: _Components | None = None


def current_recorder() -> RuntimeRecorder | None:
    """Return the active recorder, or ``None`` when trace is disabled."""
    return trace_state.current_recorder()


def _runtime_root() -> Path:
    from jiuwenswarm.common.utils import get_user_workspace_dir

    return get_user_workspace_dir() / ".trace" / "runtime"


def _replay_early_marks(recorder: RuntimeRecorder, clock: ClockBase) -> None:
    """Replay Worker marks exactly once as spans under the startup root."""
    if clock.replayed:
        return
    clock.replayed = True
    marks = list(clock.early_marks)
    for previous, current in zip(marks, marks[1:]):
        phase = _EARLY_PHASE_NAMES.get(current[0])
        if phase is None:
            continue
        failed = (
            current[0] == "preload"
            and getattr(clock, "failed_phase", None) == "preload"
        )
        attributes: dict[str, Any] = {"runtime.stage": current[0]}
        if failed:
            attributes["runtime.reason_code"] = "runtime_load_failed"
        handle = recorder.start(
            phase,
            parent=recorder.startup_handle,
            level="normal",
            mono_start=previous[1],
            attributes=attributes,
        )
        recorder.end(
            handle,
            "error" if failed else "success",
            mono_end=current[1],
            attributes=attributes,
        )


def attach_runtime_trace(
    *,
    role: str,
    settings: RuntimeTraceSettings,
    clock: ClockBase | None = None,
    worker_instance: str | None = None,
    generation: int | None = None,
    startup_start_mono_ns: int | None = None,
) -> RuntimeRecorder | None:
    """Assemble the process-local recorder and open the startup root.

    Returns ``None`` when the feature is disabled or another recorder is
    already installed. Failures are logged and swallowed: tracing must never
    break the process it observes.
    """
    if not settings.enabled:
        return None
    existing = trace_state.current_recorder()
    if existing is not None:
        return existing

    clock = clock or ClockBase()
    run_id = uuid.uuid4().hex
    pid = os.getpid()
    service_name = _service_name_for(settings.service_name, role)
    start_mono = (
        startup_start_mono_ns
        if isinstance(startup_start_mono_ns, int)
        else clock.mono_ns
    )
    # meta.started_at is the process entry, the same instant as the startup
    # root. Using the attach-time wall clock here would offset Front's metadata
    # by the whole import phase.
    started_at_ns = clock.to_unix_nano(start_mono)

    writer: RuntimeTraceWriter | None = None
    processor: Any = None
    sink: RuntimeRecordSink | None = None
    try:
        store = RuntimeTraceStore(
            root=_runtime_root(),
            role=role,
            run_id=run_id,
            pid=pid,
            started_at_ns=started_at_ns,
            retention_days=settings.retention_days,
        )
        provider, processor = build_runtime_tracer(service_name, settings)
        tracer = provider.get_tracer(service_name)
        kinds = SpanKinds()
        writer = RuntimeTraceWriter(store, retention_days=settings.retention_days)
        sink = RuntimeRecordSink(writer, kinds)
        base_attrs: dict[str, Any] = {
            "runtime.scope": "runtime",
            "runtime.run_id": run_id,
            "runtime.role": role,
            "runtime.pid": pid,
        }
        if worker_instance:
            base_attrs["runtime.worker_instance"] = worker_instance
        if generation is not None:
            base_attrs["runtime.generation"] = generation

        recorder = RuntimeRecorder(
            tracer=tracer,
            kinds=kinds,
            clock=clock,
            base_attrs=base_attrs,
            root_context=resolve_root_context(),
            on_worker_meta=writer.set_worker_meta,
        )
        if worker_instance is not None or generation is not None:
            writer.set_worker_meta(worker_instance, generation)

        processor.register_consumer(sink)
        writer.start()

        recorder.startup_handle = recorder.start(
            "startup", level="root", mono_start=start_mono
        )
        if role == "worker":
            _replay_early_marks(recorder, clock)
    except Exception:  # noqa: BLE001 - assembly failure keeps business running
        logger.warning("runtime trace: attach failed; trace disabled", exc_info=True)
        if processor is not None and sink is not None:
            try:
                processor.remove_consumer(sink, timeout_millis=100)
            except Exception:  # noqa: BLE001
                pass
        if writer is not None:
            try:
                writer.request_stop()
                writer.join(timeout=0.5)
            except Exception:  # noqa: BLE001
                pass
        return None

    components = _Components(
        recorder=recorder,
        provider=provider,
        processor=processor,
        sink=sink,
        writer=writer,
        clock=clock,
        settings=settings,
        role=role,
        run_id=run_id,
    )
    global _components
    with _lock:
        _components = components
    trace_state.set_state(recorder, settings)
    logger.info(
        "runtime trace: attached role=%s run_id=%s service=%s",
        role,
        run_id,
        service_name,
    )
    return recorder


def start_shutdown_root() -> None:
    """Open the shutdown root; a no-op when trace is disabled."""
    recorder = current_recorder()
    if recorder is None or recorder.shutdown_handle is not None:
        return
    recorder.shutdown_handle = recorder.start("shutdown", level="root")


def close_open_startup_root(
    *,
    outcome: str | None = None,
    reason_code: str | None = None,
) -> None:
    """Finish a startup root that is still open.

    Explicit ``outcome``/``reason_code`` win (bind failure, IPC timeout,
    cancellation). Otherwise the runtime length decides between the ready
    timeout failure and a plain cancellation.
    """
    recorder = current_recorder()
    if recorder is None:
        return
    handle = recorder.startup_handle
    if handle is None or handle.ended:
        return
    if outcome is None:
        elapsed_ns = time.monotonic_ns() - handle.mono_start
        if elapsed_ns > _READY_TIMEOUT_SECONDS * 1_000_000_000:
            outcome, reason_code = "error", "ready_timeout"
        else:
            outcome, reason_code = "cancelled", "cancel_requested"
    attributes = (
        {"runtime.reason_code": reason_code} if reason_code else None
    )
    recorder.end(
        handle,
        outcome,
        mono_end=time.monotonic_ns(),
        attributes=attributes,
    )


def shutdown_runtime_trace(
    *,
    deadline_mono: float | None = None,
    outcome: str = "success",
    reason_code: str | None = None,
) -> bool:
    """End the shutdown root, unregister, and drain within one deadline.

    Returns whether the local writer drained before the deadline. Export flush
    failures never change that answer; unsent batches are discarded.
    """
    components = _components
    if components is None:
        return True
    started = time.monotonic()
    budget = _DRAIN_CAP_SECONDS
    if deadline_mono is not None:
        budget = min(budget, max(0.0, deadline_mono - started))
    deadline = started + budget

    recorder = components.recorder
    if recorder.shutdown_handle is not None and not recorder.shutdown_handle.ended:
        attributes = (
            {"runtime.reason_code": reason_code} if reason_code else None
        )
        try:
            recorder.end(
                recorder.shutdown_handle,
                outcome,
                mono_end=time.monotonic_ns(),
                attributes=attributes,
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "runtime trace: shutdown root end failed", exc_info=True
            )

    remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
    try:
        components.processor.remove_consumer(
            components.sink, timeout_millis=remaining_ms
        )
    except Exception:  # noqa: BLE001 - draining still makes progress
        logger.warning(
            "runtime trace: consumer unregister exceeded budget", exc_info=True
        )

    components.writer.request_stop()
    components.writer.join(timeout=max(0.0, deadline - time.monotonic()))

    if components.provider is not None:
        _flush_exporter_with_budget(components.provider, deadline)
    return not components.writer.is_alive()


def _flush_exporter_with_budget(provider: Any, deadline: float) -> None:
    """Best-effort export flush bounded by the shared shutdown deadline."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return

    def _flush() -> None:
        try:
            # ``timeout_millis`` is ignored by OTel 1.35+, so the outer join
            # decides when to give up. ``provider.shutdown`` is never called:
            # its BatchSpanProcessor join has no usable budget.
            provider.force_flush(timeout_millis=int(remaining * 1000))
        except Exception:  # noqa: BLE001
            logger.warning("runtime trace: exporter flush failed", exc_info=True)

    thread = threading.Thread(target=_flush, name="runtime-trace-flush", daemon=True)
    thread.start()
    thread.join(timeout=max(0.0, deadline - time.monotonic()))
    if thread.is_alive():
        logger.warning(
            "runtime trace: exporter flush exceeded budget; unsent batches dropped"
        )


def attach_runtime_trace_on_failure(
    *,
    settings: RuntimeTraceSettings | None,
    clock: ClockBase | None,
    worker_instance: str | None,
    generation: int | None,
    reason_code: str,
) -> None:
    """Finish tracing after a Worker startup failure (idempotent, one-shot).

    An installed recorder only finishes its startup root and drains; a missing
    recorder is assembled, replayed from the early marks, given an error root,
    and drained. The global recorder is cleared afterwards so later signals
    and the ``serve_worker`` finally become silent.
    """
    recorder = current_recorder()
    if recorder is None:
        if (
            settings is None
            or not getattr(settings, "enabled", False)
            or clock is None
        ):
            return
        try:
            attach_runtime_trace(
                role="worker",
                settings=settings,
                clock=clock,
                worker_instance=worker_instance,
                generation=generation,
            )
        except Exception:  # noqa: BLE001 - failure path keeps its original error
            logger.warning(
                "runtime trace: failure attach skipped", exc_info=True
            )
            return

    close_open_startup_root(outcome="error", reason_code=reason_code)
    shutdown_runtime_trace(
        deadline_mono=time.monotonic() + _DRAIN_CAP_SECONDS,
        outcome="error",
        reason_code=reason_code,
    )
    _reset()


def _reset() -> None:
    """Forget the recorder after a one-shot failure drain."""
    global _components
    with _lock:
        components = _components
        _components = None
    if components is not None:
        trace_state.clear_state(components.recorder)
