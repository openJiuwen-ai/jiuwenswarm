# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Thread-safe post-commit trajectory update hints."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from jiuwenswarm.observability.models import CommittedTraceUpdate

logger = logging.getLogger(__name__)

TrajectoryUpdateListener = Callable[[tuple[CommittedTraceUpdate, ...]], None]


class TrajectoryUpdateBroker:
    """Fan out committed revision hints without carrying raw trace payloads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._listeners: set[TrajectoryUpdateListener] = set()

    def register(self, listener: TrajectoryUpdateListener) -> None:
        """Register one listener by callable identity."""
        with self._lock:
            self._listeners.add(listener)

    def unregister(self, listener: TrajectoryUpdateListener) -> None:
        """Unregister one listener by callable identity."""
        with self._lock:
            self._listeners.discard(listener)

    def publish(self, updates: tuple[CommittedTraceUpdate, ...]) -> None:
        """Publish immutable post-commit hints with listener isolation."""
        if not updates:
            return
        with self._lock:
            listeners = tuple(self._listeners)
        for listener in listeners:
            try:
                listener(updates)
            except Exception:
                logger.exception("Trajectory update listener failed")


trajectory_update_broker = TrajectoryUpdateBroker()


__all__ = [
    "TrajectoryUpdateBroker",
    "TrajectoryUpdateListener",
    "trajectory_update_broker",
]
