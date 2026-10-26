"""Session metrics backed by the real in-flight asyncio tasks."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from threading import RLock
from typing import Any

from jiuwenswarm.telemetry.attributes import (
    JIUWENCLAW_SESSION_ID,
    JIUWENCLAW_SESSION_STATE,
    JIUWENCLAW_SESSION_STATE_REASON,
)
from jiuwenswarm.telemetry.metrics import TelemetryMetrics

logger = logging.getLogger(__name__)


@dataclass
class InFlightSession:
    """Telemetry state for one real SessionManager task generation."""

    generation: int
    started_at: float
    state: str
    stuck_reported: bool = False


@dataclass(frozen=True)
class _MetricFacadeBinding:
    metrics: TelemetryMetrics | Any
    stuck_threshold_ms: float
    stuck_check_interval_s: float


@dataclass
class _MetricProviderBinding:
    identity: object
    facades: dict[int, _MetricFacadeBinding]
    reported_sessions: set[str] = field(default_factory=set)

    def representative(self) -> TelemetryMetrics | Any:
        return next(iter(self.facades.values())).metrics

    def stuck_threshold_ms(self) -> float:
        return min(facade.stuck_threshold_ms for facade in self.facades.values())

    def stuck_check_interval_s(self) -> float:
        return min(facade.stuck_check_interval_s for facade in self.facades.values())


class SessionTelemetry:
    """Record session transitions without replacing SessionManager ownership."""

    def __init__(
        self,
        *,
        metrics: TelemetryMetrics | Any | None = None,
        stuck_threshold_ms: float = 300000.0,
        stuck_check_interval_s: float = 30.0,
    ) -> None:
        self._lock = RLock()
        self._metric_bindings: dict[int, _MetricProviderBinding] = {}
        self._default_stuck_check_interval_s = max(float(stuck_check_interval_s), 0.001)
        self._stuck_check_interval_s = self._default_stuck_check_interval_s
        self._next_periodic_check_at = 0.0
        self._generation = 0
        self._in_flight: dict[
            tuple[str, int], tuple[asyncio.Task[Any], InFlightSession]
        ] = {}
        if metrics is not None:
            self.configure(
                metrics=metrics,
                stuck_threshold_ms=stuck_threshold_ms,
                stuck_check_interval_s=stuck_check_interval_s,
            )

    def configure(
        self,
        *,
        metrics: TelemetryMetrics | Any,
        stuck_threshold_ms: float,
        stuck_check_interval_s: float,
    ) -> None:
        """Bind the active runtime metrics while preserving live task state."""
        threshold = max(float(stuck_threshold_ms), 0.0)
        interval = max(float(stuck_check_interval_s), 0.001)
        identity = _metric_provider_identity(metrics)
        provider_key = id(identity)
        should_bind_observer = False
        with self._lock:
            binding = self._metric_bindings.get(provider_key)
            if binding is None or binding.identity is not identity:
                binding = _MetricProviderBinding(
                    identity=identity,
                    facades={},
                )
                self._metric_bindings[provider_key] = binding
                should_bind_observer = True
            binding.facades[id(metrics)] = _MetricFacadeBinding(
                metrics=metrics,
                stuck_threshold_ms=threshold,
                stuck_check_interval_s=interval,
            )
            self._refresh_checker_config_locked()
        if not should_bind_observer:
            return
        try:
            metrics.set_session_active_observer(self.active_count)
        except Exception as error:
            logger.debug("[SessionTelemetry] active observer setup failed: %s", error)

    def deactivate(self, metrics: TelemetryMetrics | Any | None = None) -> None:
        """Detach one runtime's metric facade without discarding live tasks."""
        clear_observers: list[TelemetryMetrics | Any] = []
        with self._lock:
            if metrics is None:
                clear_observers = [
                    binding.representative()
                    for binding in self._metric_bindings.values()
                ]
                self._metric_bindings.clear()
            else:
                identity = _metric_provider_identity(metrics)
                provider_key = id(identity)
                binding = self._metric_bindings.get(provider_key)
                if binding is None or binding.identity is not identity:
                    return
                facade = binding.facades.get(id(metrics))
                if facade is None or facade.metrics is not metrics:
                    return
                del binding.facades[id(metrics)]
                if not binding.facades:
                    del self._metric_bindings[provider_key]
                    clear_observers.append(metrics)
            self._refresh_checker_config_locked()
        for current in clear_observers:
            try:
                current.set_session_active_observer(None)
            except Exception as error:
                logger.debug(
                    "[SessionTelemetry] active observer cleanup failed: %s",
                    error,
                )

    def session_created(self, session_id: str) -> None:
        """Record creation of one real SessionManager processor generation."""
        attributes = {JIUWENCLAW_SESSION_ID: session_id}
        self._add("jiuwenclaw.session.created.count", attributes)
        self._emit_state(session_id, "created", "new_processor")

    def task_started(self, session_id: str, task: asyncio.Task[Any]) -> int:
        """Bind a real task as the current in-flight generation."""
        if not isinstance(task, asyncio.Task):
            raise TypeError("task must be an asyncio.Task")
        with self._lock:
            self._generation += 1
            generation = self._generation
            self._in_flight[(session_id, generation)] = (
                task,
                InFlightSession(
                    generation=generation,
                    started_at=time.monotonic(),
                    state="active",
                ),
            )
        self._emit_state(session_id, "active", "task_started")
        return generation

    def task_finished(
        self,
        session_id: str,
        task: asyncio.Task[Any],
        generation: int,
        state: str,
    ) -> None:
        """Compare-and-remove one completed task generation."""
        with self._lock:
            key = (session_id, generation)
            current = self._in_flight.get(key)
            if current is None:
                return
            current_task, flight = current
            if current_task is not task or flight.generation != generation:
                return
            previous_state = flight.state
            del self._in_flight[key]
            if not any(
                tracked_session_id == session_id
                for tracked_session_id, _ in self._in_flight
            ):
                for binding in self._metric_bindings.values():
                    binding.reported_sessions.discard(session_id)

        final_state, reason = _finished_transition(state)
        if previous_state == "cancelled" and final_state == "cancelled":
            return
        self._emit_state(session_id, final_state, reason)

    def task_cancelled(
        self,
        session_id: str,
        task: asyncio.Task[Any],
        *,
        reason: str = "user_cancel",
    ) -> None:
        """Mark cancellation on the current real task without ending it early."""
        with self._lock:
            flight = None
            for (tracked_session_id, _), (
                tracked_task,
                tracked_flight,
            ) in self._in_flight.items():
                if tracked_session_id == session_id and tracked_task is task:
                    flight = tracked_flight
                    break
            if flight is None:
                return
            if flight.state == "cancelled":
                return
            flight.state = "cancelled"
        self._emit_state(session_id, "cancelled", reason)

    def active_count(self) -> int:
        """Return the number of sessions with a real in-flight task."""
        with self._lock:
            return len({session_id for session_id, _ in self._in_flight})

    def check_stuck_sessions(self, now: float | None = None) -> int:
        """Record overdue task age and return newly reported stuck sessions."""
        checked_at = time.monotonic() if now is None else float(now)
        observations: list[tuple[str, float, TelemetryMetrics | Any, bool]] = []
        with self._lock:
            bindings = list(self._metric_bindings.values())
            session_flights: dict[str, list[InFlightSession]] = {}
            for (session_id, _), (_, flight) in self._in_flight.items():
                session_flights.setdefault(session_id, []).append(flight)
            for binding in bindings:
                threshold = binding.stuck_threshold_ms()
                for session_id, flights in session_flights.items():
                    oldest_age_ms = max(
                        max((checked_at - flight.started_at) * 1000.0, 0.0)
                        for flight in flights
                    )
                    if oldest_age_ms < threshold:
                        continue
                    newly_reported = session_id not in binding.reported_sessions
                    binding.reported_sessions.add(session_id)
                    for flight in flights:
                        flight.stuck_reported = True
                    observations.append(
                        (
                            session_id,
                            oldest_age_ms,
                            binding.representative(),
                            newly_reported,
                        )
                    )

        newly_stuck: set[str] = set()
        for session_id, age_ms, metrics, newly_reported in observations:
            attributes = {JIUWENCLAW_SESSION_ID: session_id}
            self._record_metric(
                metrics,
                "jiuwenclaw.session.stuck_age_ms",
                age_ms,
                attributes,
            )
            if newly_reported:
                newly_stuck.add(session_id)
                self._add_metric(
                    metrics,
                    "jiuwenclaw.session.stuck",
                    attributes,
                )
                logger.warning(
                    "[SessionTelemetry] stuck session: session_id=%s age_ms=%.0f",
                    session_id,
                    age_ms,
                )
        return len(newly_stuck)

    async def run_stuck_checker(self, stop_event: asyncio.Event) -> None:
        """Check stuck sessions until the owning runtime requests shutdown."""
        while not stop_event.is_set():
            with self._lock:
                interval = self._stuck_check_interval_s
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except TimeoutError:
                checked_at = time.monotonic()
                with self._lock:
                    if checked_at < self._next_periodic_check_at:
                        continue
                    self._next_periodic_check_at = checked_at + interval
                self.check_stuck_sessions(checked_at)

    def _emit_state(self, session_id: str, state: str, reason: str) -> None:
        self._add(
            "jiuwenclaw.session.state",
            {
                JIUWENCLAW_SESSION_ID: session_id,
                JIUWENCLAW_SESSION_STATE: state,
                JIUWENCLAW_SESSION_STATE_REASON: reason,
            },
        )

    def _add(self, name: str, attributes: dict[str, str]) -> None:
        for metrics in self._metrics_snapshot():
            self._add_metric(metrics, name, attributes)

    @staticmethod
    def _add_metric(
        metrics: TelemetryMetrics | Any,
        name: str,
        attributes: dict[str, str],
    ) -> None:
        try:
            metrics.add(name, 1, attributes)
        except Exception as error:
            logger.debug("[SessionTelemetry] counter failed: %s", error)

    @staticmethod
    def _record_metric(
        metrics: TelemetryMetrics | Any,
        name: str,
        value: float,
        attributes: dict[str, str],
    ) -> None:
        try:
            metrics.record(name, value, attributes)
        except Exception as error:
            logger.debug("[SessionTelemetry] histogram failed: %s", error)

    def _metrics_snapshot(self) -> list[TelemetryMetrics | Any]:
        with self._lock:
            return [
                binding.representative() for binding in self._metric_bindings.values()
            ]

    def _refresh_checker_config_locked(self) -> None:
        if not self._metric_bindings:
            self._stuck_check_interval_s = self._default_stuck_check_interval_s
            self._next_periodic_check_at = 0.0
            return
        self._stuck_check_interval_s = min(
            binding.stuck_check_interval_s()
            for binding in self._metric_bindings.values()
        )
        self._next_periodic_check_at = 0.0


def _finished_transition(state: str) -> tuple[str, str]:
    normalized = str(state or "idle").strip().lower()
    if normalized == "error":
        return "idle", "task_error"
    if normalized in {"cancelled", "closed"}:
        return "cancelled", (
            "session_closed" if normalized == "closed" else "task_cancelled"
        )
    return "idle", "task_completed"


def _metric_provider_identity(metrics: TelemetryMetrics | Any) -> object:
    try:
        identity = metrics.session_active_observer_identity
    except Exception:
        identity = None
    return metrics if identity is None else identity


_SESSION_TELEMETRY = SessionTelemetry()


def get_session_telemetry() -> SessionTelemetry:
    """Return the process-wide facade used by every SessionManager instance."""
    return _SESSION_TELEMETRY


__all__ = [
    "InFlightSession",
    "SessionTelemetry",
    "get_session_telemetry",
]
