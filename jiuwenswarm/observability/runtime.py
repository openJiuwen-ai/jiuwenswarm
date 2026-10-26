# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentServer lifecycle for the Core OTLP record consumer."""

from __future__ import annotations

import logging
import threading
from typing import Protocol

from jiuwenswarm.observability.config import (
    TrajectoryStoreSettings,
    load_trajectory_store_settings,
)
from jiuwenswarm.observability.gateway_hints import trajectory_gateway_hint_bridge
from jiuwenswarm.observability.session_delete import (
    set_trajectory_session_delete_backend,
)
from jiuwenswarm.observability.sink import (
    CommitCallback,
    TrajectorySessionSinkRouter,
)
from jiuwenswarm.observability.updates import trajectory_update_broker

logger = logging.getLogger(__name__)


class _SpanRecordProcessorLike(Protocol):
    def register_consumer(self, consumer: TrajectorySessionSinkRouter) -> None:
        """Register one sink by object identity."""
        ...

    def unregister_consumer(self, consumer: TrajectorySessionSinkRouter) -> None:
        """Unregister one sink by object identity."""
        ...


_runtime_lock = threading.RLock()
_runtime_sink: TrajectorySessionSinkRouter | None = None
_runtime_processor: _SpanRecordProcessorLike | None = None
_runtime_settings: TrajectoryStoreSettings | None = None
_runtime_demands: set[str] = set()


def sync_trajectory_runtime(
    settings: TrajectoryStoreSettings | None = None,
    *,
    on_commit: CommitCallback | None = None,
    demand: str | None = None,
) -> TrajectorySessionSinkRouter | None:
    """Synchronize the AgentServer sink with the current trajectory settings."""
    resolved = settings or load_trajectory_store_settings()
    normalized_demand = str(demand or "").strip()
    if not resolved.enabled:
        shutdown_trajectory_runtime(demand=normalized_demand or None)
        return None
    if normalized_demand:
        with _runtime_lock:
            _runtime_demands.add(normalized_demand)
    try:
        return start_trajectory_runtime(resolved, on_commit=on_commit)
    except Exception:
        if normalized_demand:
            with _runtime_lock:
                _runtime_demands.discard(normalized_demand)
        raise


def start_trajectory_runtime(
    settings: TrajectoryStoreSettings | None = None,
    *,
    on_commit: CommitCallback | None = None,
) -> TrajectorySessionSinkRouter | None:
    """Start and register the process-wide sink once.

    The sink is ready before it is exposed to Core. A settings change performs
    the inverse lifecycle first: unregister, drain, close, then replace.
    """
    global _runtime_processor, _runtime_settings, _runtime_sink

    resolved = settings or load_trajectory_store_settings()
    if not resolved.enabled:
        shutdown_trajectory_runtime()
        return None
    with _runtime_lock:
        if (
            _runtime_sink is not None
            and _runtime_processor is not None
            and _runtime_settings == resolved
        ):
            _runtime_sink.set_commit_callback(_combined_commit_callback(on_commit))
            return _runtime_sink
        if _runtime_sink is not None or _runtime_processor is not None:
            if not _shutdown_locked(clear_demands=False):
                raise RuntimeError("Previous trajectory runtime did not stop cleanly")

        sink = _create_sink(
            resolved,
            on_commit=_combined_commit_callback(on_commit),
        )
        try:
            sink.start()
            processor = _get_core_span_record_processor()
            processor.register_consumer(sink)
        except Exception:
            try:
                sink.close()
            except Exception:
                logger.exception("Trajectory runtime startup cleanup failed")
            raise
        set_trajectory_session_delete_backend(sink)
        _runtime_sink = sink
        _runtime_processor = processor
        _runtime_settings = resolved
        logger.info(
            "Trajectory runtime enabled: database=%s queue_size=%d",
            resolved.database_path,
            resolved.queue_size,
        )
        return sink


def shutdown_trajectory_runtime(
    *,
    timeout: float = 15.0,
    demand: str | None = None,
) -> bool:
    """Unregister first, then drain and close the process-wide sink."""
    with _runtime_lock:
        normalized_demand = str(demand or "").strip()
        if normalized_demand:
            _runtime_demands.discard(normalized_demand)
            if _runtime_demands:
                return True
        return _shutdown_locked(timeout=timeout)


def get_trajectory_runtime_sink() -> TrajectorySessionSinkRouter | None:
    """Return the active process-local sink for diagnostics."""
    with _runtime_lock:
        return _runtime_sink


def _shutdown_locked(
    *,
    timeout: float = 15.0,
    clear_demands: bool = True,
) -> bool:
    global _runtime_processor, _runtime_settings, _runtime_sink

    sink = _runtime_sink
    processor = _runtime_processor
    if sink is None and processor is None:
        _runtime_settings = None
        if clear_demands:
            _runtime_demands.clear()
        return True

    unregistered = processor is None
    if processor is not None and sink is not None:
        try:
            processor.unregister_consumer(sink)
        except Exception:
            logger.exception("Trajectory runtime consumer unregister failed")
            # Keep the still-running sink reachable and registered. Closing it
            # here would leave a stopped singleton behind while Core may still
            # retain the consumer reference.
            return False
        else:
            _runtime_processor = None
            unregistered = True

    stopped = sink is None
    if sink is not None:
        try:
            stopped = sink.close(timeout=timeout)
        except Exception:
            logger.exception("Trajectory runtime sink shutdown failed")
            stopped = False

    if unregistered and stopped:
        set_trajectory_session_delete_backend(None)
        _runtime_sink = None
        _runtime_processor = None
        _runtime_settings = None
        if clear_demands:
            _runtime_demands.clear()
        logger.info("Trajectory runtime disabled")
        return True
    return False


def _create_sink(
    settings: TrajectoryStoreSettings,
    *,
    on_commit: CommitCallback | None,
) -> TrajectorySessionSinkRouter:
    return TrajectorySessionSinkRouter(settings, on_commit=on_commit)


def _combined_commit_callback(on_commit: CommitCallback | None) -> CommitCallback:
    def _publish(updates) -> None:
        trajectory_update_broker.publish(updates)
        trajectory_gateway_hint_bridge.publish(updates)
        if on_commit is not None:
            on_commit(updates)

    return _publish


def _get_core_span_record_processor() -> _SpanRecordProcessorLike:
    from openjiuwen.extensions.observability.demand import get_span_record_processor

    return get_span_record_processor()


__all__ = [
    "get_trajectory_runtime_sink",
    "shutdown_trajectory_runtime",
    "start_trajectory_runtime",
    "sync_trajectory_runtime",
]
