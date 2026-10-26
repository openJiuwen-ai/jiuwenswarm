# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the per-stage timing of the per-request runtime setup."""

from __future__ import annotations

import pytest

from jiuwenswarm.server.runtime.agent_adapter import interface_deep
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


class _RecordingLogger:
    """Logger double capturing the level and formatted message of each call.

    Levels are tracked separately because both breakdown branches now go to
    the same logger: which one ran is a question about level, not about which
    logger object received the line.
    """

    def __init__(self) -> None:
        self.records: list[tuple[str, tuple]] = []
        self.info_records: list[tuple[str, tuple]] = []
        self.debug_records: list[tuple[str, tuple]] = []

    def info(self, msg: str, *args) -> None:
        self.records.append((msg, args))
        self.info_records.append((msg, args))

    def debug(self, msg: str, *args, **kwargs) -> None:
        self.records.append((msg, args))
        self.debug_records.append((msg, args))


def _make_adapter(stages: list[str]) -> JiuWenSwarmDeepAdapter:
    """Create an adapter whose runtime-config stages are stubbed to record order."""
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._instance = object()

    async def _stages(runtime_config, stage_timer, *, bind_request) -> None:
        for stage in stages:
            stage_timer.mark(stage)

    adapter._apply_runtime_config_stages = _stages
    return adapter


@pytest.fixture(name="loggers")
def _loggers(monkeypatch: pytest.MonkeyPatch) -> tuple[_RecordingLogger, _RecordingLogger]:
    server_log = _RecordingLogger()
    module_log = _RecordingLogger()
    monkeypatch.setattr(interface_deep, "server_logger", server_log)
    monkeypatch.setattr(interface_deep, "logger", module_log)
    return server_log, module_log


@pytest.mark.asyncio
async def test_uninitialized_adapter_still_rejects_the_turn(loggers) -> None:
    """The timing wrapper must not swallow the not-initialized contract."""
    adapter = _make_adapter([])
    adapter._instance = None

    with pytest.raises(RuntimeError):
        await adapter._update_runtime_config(object())


@pytest.mark.asyncio
async def test_stage_breakdown_is_logged_for_the_turn(loggers) -> None:
    """Every stage that ran must appear in the emitted breakdown."""
    server_log, module_log = loggers
    adapter = _make_adapter(["cwd_seed", "rails_for_mode", "session_tools"])
    runtime_config = type("_Cfg", (), {"session_id": "sess_a", "mode": "agent"})()

    await adapter._update_runtime_config(runtime_config)

    records = server_log.records + module_log.records
    assert len(records) == 1
    breakdown = records[0][1][-1]
    assert "cwd_seed=" in breakdown
    assert "rails_for_mode=" in breakdown
    assert "session_tools=" in breakdown


@pytest.mark.asyncio
async def test_slow_turn_is_reported_at_info(loggers, monkeypatch: pytest.MonkeyPatch) -> None:
    """Crossing the threshold routes the breakdown to the server log stream."""
    server_log, module_log = loggers
    monkeypatch.setattr(interface_deep, "_SLOW_RUNTIME_CONFIG_MS", 0.0)
    adapter = _make_adapter(["cwd_seed"])
    runtime_config = type("_Cfg", (), {"session_id": "sess_a", "mode": "agent"})()

    await adapter._update_runtime_config(runtime_config)

    assert len(server_log.info_records) == 1
    assert server_log.debug_records == []


@pytest.mark.asyncio
async def test_fast_turn_stays_at_debug(loggers, monkeypatch: pytest.MonkeyPatch) -> None:
    """A cheap turn must not add an INFO line to every request."""
    server_log, module_log = loggers
    monkeypatch.setattr(interface_deep, "_SLOW_RUNTIME_CONFIG_MS", 10_000.0)
    adapter = _make_adapter(["cwd_seed"])
    runtime_config = type("_Cfg", (), {"session_id": "sess_a", "mode": "agent"})()

    await adapter._update_runtime_config(runtime_config)

    assert server_log.info_records == []
    assert len(server_log.debug_records) == 1


@pytest.mark.asyncio
async def test_failing_stage_still_reports_how_far_it_got(loggers) -> None:
    """A raising turn is exactly when the breakdown matters most."""
    server_log, module_log = loggers
    adapter = _make_adapter([])

    async def _stages(runtime_config, stage_timer, *, bind_request) -> None:
        stage_timer.mark("cwd_seed")
        stage_timer.mark("rail_setters")
        raise ValueError("boom")

    adapter._apply_runtime_config_stages = _stages
    runtime_config = type("_Cfg", (), {"session_id": "sess_a", "mode": "agent"})()

    with pytest.raises(ValueError):
        await adapter._update_runtime_config(runtime_config)

    records = server_log.records + module_log.records
    assert len(records) == 1
    breakdown = records[0][1][-1]
    assert "cwd_seed=" in breakdown
    assert "rail_setters=" in breakdown
    # The stage that raised never closed, so it must not appear as completed.
    assert "runtime_state=" not in breakdown
