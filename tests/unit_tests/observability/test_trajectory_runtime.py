# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the process-wide Core consumer registration lifecycle."""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from jiuwenswarm.observability.config import TrajectoryStoreSettings
from jiuwenswarm.observability import runtime

test_logger = logging.getLogger("tests.trajectory_runtime")


class _FakeSink:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.callback: Any = None

    def start(self) -> None:
        self.events.append("sink.start")

    def set_commit_callback(self, callback: Any) -> None:
        self.callback = callback

    def close(self, *, timeout: float = 15.0) -> bool:
        self.events.append("sink.close")
        return True


class _FakeProcessor:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.consumers: list[object] = []

    def register_consumer(self, consumer: object) -> None:
        self.events.append("processor.register")
        if all(existing is not consumer for existing in self.consumers):
            self.consumers.append(consumer)

    def unregister_consumer(self, consumer: object) -> None:
        self.events.append("processor.unregister")
        self.consumers = [existing for existing in self.consumers if existing is not consumer]


class _FailingOnceProcessor(_FakeProcessor):
    def __init__(self, events: list[str]) -> None:
        super().__init__(events)
        self.fail_unregister = True

    def unregister_consumer(self, consumer: object) -> None:
        self.events.append("processor.unregister")
        if self.fail_unregister:
            raise RuntimeError("injected unregister failure")
        self.consumers = [existing for existing in self.consumers if existing is not consumer]


def _settings(database_path: Path) -> TrajectoryStoreSettings:
    return TrajectoryStoreSettings(
        enabled=True,
        database_path=database_path,
        retention_days=7,
        queue_size=16,
        batch_size=8,
        flush_interval_ms=20,
        poll_interval_ms=2000,
    )


def test_runtime_registers_once_and_unregisters_before_drain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime.shutdown_trajectory_runtime()
    events: list[str] = []
    fake_sink = _FakeSink(events)
    fake_processor = _FakeProcessor(events)

    def _create_sink(
        settings: TrajectoryStoreSettings,
        *,
        on_commit: Any,
    ) -> _FakeSink:
        fake_sink.callback = on_commit
        return fake_sink

    monkeypatch.setattr(runtime, "_create_sink", _create_sink)
    monkeypatch.setattr(
        runtime,
        "_get_core_span_record_processor",
        lambda: fake_processor,
    )
    callback = lambda _updates: None

    try:
        first = runtime.start_trajectory_runtime(
            _settings(tmp_path / "trajectory.sqlite3"),
            on_commit=callback,
        )
        second = runtime.start_trajectory_runtime(
            _settings(tmp_path / "trajectory.sqlite3"),
            on_commit=callback,
        )

        assert first is fake_sink
        assert second is fake_sink
        assert fake_processor.consumers == [fake_sink]
        assert events == ["sink.start", "processor.register"]
        assert runtime.shutdown_trajectory_runtime() is True
        assert events == [
            "sink.start",
            "processor.register",
            "processor.unregister",
            "sink.close",
        ]
        assert runtime.get_trajectory_runtime_sink() is None
    finally:
        runtime.shutdown_trajectory_runtime()
    test_logger.info("runtime registration was idempotent and shutdown order was safe")


def test_disabled_sync_stops_existing_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime.shutdown_trajectory_runtime()
    events: list[str] = []
    fake_sink = _FakeSink(events)
    fake_processor = _FakeProcessor(events)
    settings = _settings(tmp_path / "trajectory.sqlite3")

    monkeypatch.setattr(
        runtime,
        "_create_sink",
        lambda _settings, on_commit: fake_sink,
    )
    monkeypatch.setattr(
        runtime,
        "_get_core_span_record_processor",
        lambda: fake_processor,
    )

    try:
        assert runtime.sync_trajectory_runtime(settings) is fake_sink
        assert runtime.sync_trajectory_runtime(replace(settings, enabled=False)) is None
        assert events == [
            "sink.start",
            "processor.register",
            "processor.unregister",
            "sink.close",
        ]
    finally:
        runtime.shutdown_trajectory_runtime()
    test_logger.info("disabled configuration stopped and drained the active runtime")


def test_unregister_failure_keeps_running_sink_for_later_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime.shutdown_trajectory_runtime()
    events: list[str] = []
    fake_sink = _FakeSink(events)
    fake_processor = _FailingOnceProcessor(events)
    settings = _settings(tmp_path / "trajectory.sqlite3")

    monkeypatch.setattr(
        runtime,
        "_create_sink",
        lambda _settings, on_commit: fake_sink,
    )
    monkeypatch.setattr(
        runtime,
        "_get_core_span_record_processor",
        lambda: fake_processor,
    )

    try:
        assert runtime.start_trajectory_runtime(settings) is fake_sink
        assert runtime.shutdown_trajectory_runtime(timeout=3) is False
        assert runtime.get_trajectory_runtime_sink() is fake_sink
        assert fake_processor.consumers == [fake_sink]
        assert events == [
            "sink.start",
            "processor.register",
            "processor.unregister",
        ]

        fake_processor.fail_unregister = False
        assert runtime.shutdown_trajectory_runtime(timeout=3) is True
        assert runtime.get_trajectory_runtime_sink() is None
        assert events == [
            "sink.start",
            "processor.register",
            "processor.unregister",
            "processor.unregister",
            "sink.close",
        ]
    finally:
        fake_processor.fail_unregister = False
        runtime.shutdown_trajectory_runtime()
    test_logger.info("unregister failure left the running singleton available for retry")
