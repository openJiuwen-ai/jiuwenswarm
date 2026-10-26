"""Session metrics bound to the real SessionManager task lifecycle."""

from __future__ import annotations

import asyncio
import contextvars
import time
from unittest.mock import Mock

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from jiuwenswarm.server.runtime.session.session_manager import SessionManager
from jiuwenswarm.telemetry.attributes import (
    JIUWENCLAW_SESSION_ID,
    JIUWENCLAW_SESSION_STATE,
    JIUWENCLAW_SESSION_STATE_REASON,
)
from jiuwenswarm.telemetry.session import SessionTelemetry
from jiuwenswarm.telemetry.metrics import TelemetryMetrics


def _counter_calls(metrics: Mock, name: str) -> list[object]:
    return [call for call in metrics.add.call_args_list if call.args[0] == name]


def _histogram_calls(metrics: Mock, name: str) -> list[object]:
    return [call for call in metrics.record.call_args_list if call.args[0] == name]


def _states(metrics: Mock) -> list[tuple[str, str]]:
    return [
        (
            call.args[2][JIUWENCLAW_SESSION_STATE],
            call.args[2][JIUWENCLAW_SESSION_STATE_REASON],
        )
        for call in _counter_calls(metrics, "jiuwenclaw.session.state")
    ]


def _metric_value(reader: InMemoryMetricReader, name: str) -> int | float:
    data = reader.get_metrics_data()
    matches = [
        metric
        for resource_metrics in ([] if data is None else data.resource_metrics)
        for scope_metrics in resource_metrics.scope_metrics
        for metric in scope_metrics.metrics
        if metric.name == name
    ]
    assert len(matches) == 1
    points = matches[0].data.data_points
    assert len(points) == 1
    return points[0].value


@pytest.mark.asyncio
async def test_real_manager_records_five_metrics_and_keeps_queue_quadruple() -> None:
    metrics = Mock()
    telemetry = SessionTelemetry(
        metrics=metrics,
        stuck_threshold_ms=1.0,
        stuck_check_interval_s=0.001,
    )
    manager = SessionManager(telemetry=telemetry)
    started = asyncio.Event()

    async def blocking_task() -> None:
        started.set()
        await asyncio.Event().wait()

    async def queued_task() -> None:
        raise AssertionError("queued work must not run during terminal close")

    await manager.submit_task("session-real", blocking_task)
    await asyncio.wait_for(started.wait(), timeout=1)
    await manager.submit_task("session-real", queued_task)
    queue = manager._session_queues["session-real"]
    queued_item = queue.get_nowait()
    try:
        assert len(queued_item) == 4
        priority, task_func, task_context, result_future = queued_item
        assert isinstance(priority, int)
        assert task_func is queued_task
        assert isinstance(task_context, contextvars.Context)
        assert result_future is None
    finally:
        queue.task_done()
        queue.put_nowait(queued_item)

    metrics.set_session_active_observer.assert_called_once()
    observer = metrics.set_session_active_observer.call_args.args[0]
    assert observer() == telemetry.active_count() == 1
    assert len(_counter_calls(metrics, "jiuwenclaw.session.created.count")) == 1
    assert ("created", "new_processor") in _states(metrics)
    assert ("active", "task_started") in _states(metrics)

    assert telemetry.check_stuck_sessions(now=time.monotonic() + 1.0) == 1
    assert len(_counter_calls(metrics, "jiuwenclaw.session.stuck")) == 1
    stuck_age = _histogram_calls(metrics, "jiuwenclaw.session.stuck_age_ms")[0]
    assert stuck_age.args[1] >= 1.0
    assert stuck_age.args[2][JIUWENCLAW_SESSION_ID] == "session-real"

    assert await manager.close_session("session-real", wait_timeout=0.01) is True
    assert telemetry.active_count() == 0
    assert ("cancelled", "session_closed") in _states(metrics)
    assert observer() == 0


@pytest.mark.asyncio
async def test_cancel_session_task_clears_real_in_flight_state() -> None:
    metrics = Mock()
    telemetry = SessionTelemetry(metrics=metrics)
    manager = SessionManager(telemetry=telemetry)
    started = asyncio.Event()

    async def blocking_task() -> None:
        started.set()
        await asyncio.Event().wait()

    await manager.submit_task("session-cancel", blocking_task)
    await asyncio.wait_for(started.wait(), timeout=1)
    task = manager.get_current_task("session-cancel")

    await manager.cancel_session_task("session-cancel")

    assert task is not None and task.cancelled()
    assert telemetry.active_count() == 0
    assert ("cancelled", "user_cancel") in _states(metrics)


@pytest.mark.asyncio
async def test_external_scheduler_task_keeps_session_metrics_without_taking_ownership() -> (
    None
):
    metrics = Mock()
    telemetry = SessionTelemetry(metrics=metrics, stuck_threshold_ms=1.0)
    manager = SessionManager(telemetry=telemetry)
    release = asyncio.Event()

    async def externally_scheduled() -> None:
        await release.wait()

    task = asyncio.create_task(externally_scheduled())
    manager.observe_external_task("session-external", task)

    assert manager.get_current_task("session-external") is None
    assert manager.has_session_runtime("session-external") is False
    assert telemetry.active_count() == 1
    assert len(_counter_calls(metrics, "jiuwenclaw.session.created.count")) == 1
    assert ("created", "new_processor") in _states(metrics)
    assert ("active", "task_started") in _states(metrics)
    assert telemetry.check_stuck_sessions(now=time.monotonic() + 1.0) == 1

    release.set()
    await task
    await asyncio.sleep(0)

    assert telemetry.active_count() == 0
    assert ("idle", "task_completed") in _states(metrics)


@pytest.mark.asyncio
async def test_old_task_cannot_clear_replacement_generation() -> None:
    metrics = Mock()
    telemetry = SessionTelemetry(metrics=metrics)
    first = asyncio.create_task(asyncio.Event().wait())
    second = asyncio.create_task(asyncio.Event().wait())
    try:
        first_generation = telemetry.task_started("same-session", first)
        second_generation = telemetry.task_started("same-session", second)

        telemetry.task_finished("same-session", first, first_generation, "idle")
        assert telemetry.active_count() == 1

        telemetry.task_finished("same-session", second, second_generation, "idle")
        assert telemetry.active_count() == 0
    finally:
        first.cancel()
        second.cancel()
        await asyncio.gather(first, second, return_exceptions=True)


@pytest.mark.asyncio
async def test_reconnected_generation_does_not_hide_stubborn_old_task() -> None:
    metrics = Mock()
    telemetry = SessionTelemetry(metrics=metrics, stuck_threshold_ms=1.0)
    manager = SessionManager(telemetry=telemetry)
    old_started = asyncio.Event()
    old_cancelling = asyncio.Event()
    allow_old_exit = asyncio.Event()
    replacement_completed = asyncio.Event()

    async def stubborn_old_task() -> None:
        old_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            old_cancelling.set()
            await allow_old_exit.wait()
            raise

    async def replacement_task() -> None:
        replacement_completed.set()

    await manager.submit_task("session-reconnect", stubborn_old_task)
    await asyncio.wait_for(old_started.wait(), timeout=1)
    old_processor = manager._session_processors["session-reconnect"]

    try:
        assert (
            await manager.close_session("session-reconnect", wait_timeout=0.01) is True
        )
        await asyncio.wait_for(old_cancelling.wait(), timeout=1)
        assert old_processor.done() is False
        assert telemetry.active_count() == 1

        await manager.submit_task("session-reconnect", replacement_task)
        await asyncio.wait_for(replacement_completed.wait(), timeout=1)
        for _ in range(100):
            if manager.get_current_task("session-reconnect") is None:
                break
            await asyncio.sleep(0)
        else:
            raise AssertionError("replacement generation did not become idle")

        assert old_processor.done() is False
        assert telemetry.active_count() == 1
        assert telemetry.check_stuck_sessions(time.monotonic() + 1.0) == 1
    finally:
        allow_old_exit.set()
        await asyncio.gather(old_processor, return_exceptions=True)
        await manager.close_session("session-reconnect")

    assert telemetry.active_count() == 0


def test_metric_bindings_are_isolated_between_runtime_owners() -> None:
    metrics_a = Mock()
    metrics_b = Mock()
    telemetry = SessionTelemetry()
    telemetry.configure(
        metrics=metrics_a,
        stuck_threshold_ms=10.0,
        stuck_check_interval_s=1.0,
    )
    telemetry.configure(
        metrics=metrics_b,
        stuck_threshold_ms=10.0,
        stuck_check_interval_s=1.0,
    )

    telemetry.session_created("shared-before-stop")
    assert len(_counter_calls(metrics_a, "jiuwenclaw.session.created.count")) == 1
    assert len(_counter_calls(metrics_b, "jiuwenclaw.session.created.count")) == 1

    telemetry.deactivate(metrics_b)
    telemetry.session_created("owned-by-a")

    assert len(_counter_calls(metrics_a, "jiuwenclaw.session.created.count")) == 2
    assert len(_counter_calls(metrics_b, "jiuwenclaw.session.created.count")) == 1
    metrics_a.set_session_active_observer.assert_called_once()
    metrics_b.set_session_active_observer.assert_called_with(None)


@pytest.mark.asyncio
async def test_same_provider_facades_do_not_double_count_or_clear_gauge() -> None:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    telemetry = SessionTelemetry()
    metrics_a = TelemetryMetrics(provider)
    metrics_b = TelemetryMetrics(provider)
    task = asyncio.create_task(asyncio.Event().wait())
    try:
        assert (
            metrics_a.session_active_observer_identity
            is metrics_b.session_active_observer_identity
        )
        telemetry.configure(
            metrics=metrics_a,
            stuck_threshold_ms=10.0,
            stuck_check_interval_s=1.0,
        )
        telemetry.configure(
            metrics=metrics_b,
            stuck_threshold_ms=10.0,
            stuck_check_interval_s=1.0,
        )
        telemetry.session_created("same-provider")
        generation = telemetry.task_started("same-provider", task)

        assert _metric_value(reader, "jiuwenclaw.session.created.count") == 1
        assert _metric_value(reader, "jiuwenclaw.session.active") == 1

        telemetry.deactivate(metrics_b)
        assert _metric_value(reader, "jiuwenclaw.session.active") == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if "generation" in locals():
            telemetry.task_finished("same-provider", task, generation, "cancelled")
        telemetry.deactivate(metrics_a)
        provider.shutdown()


@pytest.mark.asyncio
async def test_stuck_threshold_and_first_report_are_provider_scoped() -> None:
    metrics_slow = Mock()
    metrics_fast = Mock()
    metrics_slow.session_active_observer_identity = object()
    metrics_fast.session_active_observer_identity = object()
    telemetry = SessionTelemetry()
    telemetry.configure(
        metrics=metrics_slow,
        stuck_threshold_ms=1000.0,
        stuck_check_interval_s=1.0,
    )
    telemetry.configure(
        metrics=metrics_fast,
        stuck_threshold_ms=1.0,
        stuck_check_interval_s=0.001,
    )
    task = asyncio.create_task(asyncio.Event().wait())
    generation = telemetry.task_started("provider-threshold", task)
    try:
        assert telemetry.check_stuck_sessions(time.monotonic() + 0.1) == 1
        assert not _counter_calls(metrics_slow, "jiuwenclaw.session.stuck")
        assert len(_counter_calls(metrics_fast, "jiuwenclaw.session.stuck")) == 1

        telemetry.deactivate(metrics_fast)
        assert telemetry.check_stuck_sessions(time.monotonic() + 2.0) == 1
        assert len(_counter_calls(metrics_slow, "jiuwenclaw.session.stuck")) == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        telemetry.task_finished("provider-threshold", task, generation, "cancelled")
        telemetry.deactivate(metrics_slow)


@pytest.mark.asyncio
async def test_stuck_counter_is_unique_per_provider_and_logical_session() -> None:
    metrics = Mock()
    telemetry = SessionTelemetry(metrics=metrics, stuck_threshold_ms=1.0)
    first = asyncio.create_task(asyncio.Event().wait())
    second = asyncio.create_task(asyncio.Event().wait())
    first_generation = telemetry.task_started("same-stuck-session", first)
    second_generation = telemetry.task_started("same-stuck-session", second)
    try:
        assert telemetry.active_count() == 1
        assert telemetry.check_stuck_sessions(time.monotonic() + 1.0) == 1
        assert len(_counter_calls(metrics, "jiuwenclaw.session.stuck")) == 1
        assert len(_histogram_calls(metrics, "jiuwenclaw.session.stuck_age_ms")) == 1
    finally:
        first.cancel()
        second.cancel()
        await asyncio.gather(first, second, return_exceptions=True)
        telemetry.task_finished(
            "same-stuck-session", first, first_generation, "cancelled"
        )
        telemetry.task_finished(
            "same-stuck-session", second, second_generation, "cancelled"
        )


@pytest.mark.asyncio
async def test_control_exception_is_not_reported_as_task_completed() -> None:
    class ControlSignal(BaseException):
        pass

    metrics = Mock()
    telemetry = SessionTelemetry(metrics=metrics)
    manager = SessionManager(telemetry=telemetry)

    async def control_task() -> None:
        raise ControlSignal("stop processor")

    await manager.submit_task("session-control", control_task)
    processor = manager._session_processors["session-control"]
    with pytest.raises(ControlSignal, match="stop processor"):
        await processor

    assert telemetry.active_count() == 0
    assert ("idle", "task_error") in _states(metrics)
    assert ("idle", "task_completed") not in _states(metrics)


@pytest.mark.asyncio
async def test_submit_and_wait_error_is_reported_without_killing_processor() -> None:
    metrics = Mock()
    telemetry = SessionTelemetry(metrics=metrics)
    manager = SessionManager(telemetry=telemetry)

    async def failing_task() -> None:
        raise ValueError("business failed")

    with pytest.raises(ValueError, match="business failed"):
        await manager.submit_and_wait("session-submit-error", failing_task)
    for _ in range(100):
        if manager.get_current_task("session-submit-error") is None:
            break
        await asyncio.sleep(0)
    else:
        raise AssertionError("failed submit_and_wait task was not released")

    assert manager.has_active_processor("session-submit-error") is True
    assert ("idle", "task_error") in _states(metrics)
    assert ("idle", "task_completed") not in _states(metrics)
    await manager.close_session("session-submit-error")


@pytest.mark.asyncio
async def test_metric_failures_never_break_real_session_cleanup() -> None:
    metrics = Mock()
    metrics.add.side_effect = RuntimeError("metric add failed")
    metrics.record.side_effect = RuntimeError("metric record failed")
    metrics.set_session_active_observer.side_effect = RuntimeError("observer failed")
    telemetry = SessionTelemetry(
        metrics=metrics,
        stuck_threshold_ms=0.0,
    )
    manager = SessionManager(telemetry=telemetry)
    completed = asyncio.Event()

    async def quick_task() -> None:
        completed.set()

    await manager.submit_task("session-metric-error", quick_task)
    await asyncio.wait_for(completed.wait(), timeout=1)
    for _ in range(100):
        if telemetry.active_count() == 0:
            break
        await asyncio.sleep(0)
    else:
        raise AssertionError("telemetry state retained a completed task")

    assert telemetry.check_stuck_sessions() == 0
    assert await manager.close_session("session-metric-error") is True


@pytest.mark.asyncio
async def test_stuck_checker_stops_via_runtime_event() -> None:
    metrics = Mock()
    telemetry = SessionTelemetry(
        metrics=metrics,
        stuck_threshold_ms=0.0,
        stuck_check_interval_s=0.001,
    )
    task = asyncio.create_task(asyncio.Event().wait())
    generation = telemetry.task_started("session-checker", task)
    stop_event = asyncio.Event()
    checker = asyncio.create_task(telemetry.run_stuck_checker(stop_event))
    try:
        for _ in range(100):
            if _counter_calls(metrics, "jiuwenclaw.session.stuck"):
                break
            await asyncio.sleep(0.001)
        else:
            raise AssertionError("stuck checker did not observe the active task")
        stop_event.set()
        await asyncio.wait_for(checker, timeout=1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        telemetry.task_finished("session-checker", task, generation, "cancelled")

    assert checker.done()
