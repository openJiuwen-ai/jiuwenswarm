# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the shared rail construction loop and its per-rail timing."""

from __future__ import annotations

import sys
import types

import pytest

from jiuwenswarm.server.runtime.agent_adapter import interface_deep
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
    _RailBuildInfo,
)


class _RecordingLogger:
    """Logger double capturing the level and args of each call.

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

    def warning(self, msg: str, *args, **kwargs) -> None:
        self.records.append((msg, args))


@pytest.fixture(name="loggers")
def _loggers(monkeypatch: pytest.MonkeyPatch) -> tuple[_RecordingLogger, _RecordingLogger]:
    server_log = _RecordingLogger()
    module_log = _RecordingLogger()
    monkeypatch.setattr(interface_deep, "server_logger", server_log)
    monkeypatch.setattr(interface_deep, "logger", module_log)
    monkeypatch.setattr(interface_deep, "load_hooks_config", lambda config_base: _NoHooks())
    return server_log, module_log


class _NoHooks:
    events: dict = {}


_OBSERVABILITY_RAIL_MODULE = "openjiuwen.agent_teams.observability.rail"


@pytest.fixture(autouse=True)
def _stub_observability_rail(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the standing observability rail out of these tests entirely.

    ``_instantiate_rails`` attaches it unconditionally, and both constructing it
    and merely importing its module start an OTLP exporter that leaves an
    unclosed socket behind — which pytest later surfaces as an unraisable
    exception during some unrelated test's setup. Injecting a stand-in module
    means the function-level import resolves without the real one ever loading.
    """
    stub_module = types.ModuleType(_OBSERVABILITY_RAIL_MODULE)
    stub_module.ObservabilityRail = lambda: object()
    monkeypatch.setitem(sys.modules, _OBSERVABILITY_RAIL_MODULE, stub_module)


def _make_adapter() -> JiuWenSwarmDeepAdapter:
    return object.__new__(JiuWenSwarmDeepAdapter)


def _breakdown_of(records: list[tuple[str, tuple]]) -> str:
    """Return the breakdown string of the single rail-build summary line."""
    summary = [rec for rec in records if "agent rails built" in rec[0]]
    assert len(summary) == 1
    return summary[0][1][-1]


def test_built_rails_are_assigned_and_returned(loggers) -> None:
    """Each builder's result lands on its attribute and in the returned list."""
    adapter = _make_adapter()
    first, second = object(), object()
    rail_infos = [
        _RailBuildInfo("_alpha_rail", lambda: first),
        _RailBuildInfo("_beta_rail", lambda: second),
    ]

    rails = adapter._instantiate_rails(rail_infos, {})

    assert adapter._alpha_rail is first
    assert adapter._beta_rail is second
    # The two standing rails may or may not import in this environment, so only
    # the declared ones are asserted on by identity.
    assert rails[:2] == [first, second]


def test_builder_params_are_forwarded(loggers) -> None:
    """Declared params reach the builder unchanged."""
    adapter = _make_adapter()
    seen: dict = {}

    def _build(**kwargs):
        seen.update(kwargs)
        return object()

    adapter._instantiate_rails([_RailBuildInfo("_x_rail", _build, {"config": {"k": 1}})], {})

    assert seen == {"config": {"k": 1}}


def test_none_result_is_skipped_without_failing_the_set(loggers) -> None:
    """One rail declining to build must not take the rest down with it."""
    server_log, module_log = loggers
    adapter = _make_adapter()
    survivor = object()
    rail_infos = [
        _RailBuildInfo("_absent_rail", lambda: None),
        _RailBuildInfo("_present_rail", lambda: survivor),
    ]

    rails = adapter._instantiate_rails(rail_infos, {})

    assert survivor in rails
    assert not hasattr(adapter, "_absent_rail")
    assert any("build returned None" in rec[0] for rec in module_log.records)


def test_every_rail_appears_in_the_breakdown(loggers, monkeypatch: pytest.MonkeyPatch) -> None:
    """The breakdown must name each rail, so a slow one is identifiable."""
    server_log, module_log = loggers
    monkeypatch.setattr(interface_deep, "_SLOW_RAIL_BUILD_MS", 0.0)
    adapter = _make_adapter()
    rail_infos = [
        _RailBuildInfo("_alpha_rail", lambda: object()),
        _RailBuildInfo("_beta_rail", lambda: object()),
    ]

    adapter._instantiate_rails(rail_infos, {})

    breakdown = _breakdown_of(server_log.records)
    # Attribute underscore is stripped so the breakdown reads as rail names.
    assert "alpha_rail=" in breakdown
    assert "beta_rail=" in breakdown
    assert "user_hook_rail=" in breakdown
    assert "observability_rail=" in breakdown


def test_breakdown_is_ordered_slowest_first(loggers, monkeypatch: pytest.MonkeyPatch) -> None:
    """The point of the line is the offender, so it leads."""
    server_log, module_log = loggers
    monkeypatch.setattr(interface_deep, "_SLOW_RAIL_BUILD_MS", 0.0)
    clock = {"t": 0.0}
    monkeypatch.setattr("jiuwenswarm.common.stage_timer.monotonic", lambda: clock["t"])
    adapter = _make_adapter()

    def _fast():
        clock["t"] += 0.001
        return object()

    def _slow():
        clock["t"] += 0.500
        return object()

    rail_infos = [
        _RailBuildInfo("_fast_rail", _fast),
        _RailBuildInfo("_slow_rail", _slow),
    ]

    adapter._instantiate_rails(rail_infos, {})

    breakdown = _breakdown_of(server_log.records)
    assert breakdown.startswith("slow_rail=500.0")


def test_fast_build_stays_at_debug(loggers, monkeypatch: pytest.MonkeyPatch) -> None:
    """A cheap rail set must not add an INFO line to the agent log stream."""
    server_log, module_log = loggers
    monkeypatch.setattr(interface_deep, "_SLOW_RAIL_BUILD_MS", 10_000.0)
    adapter = _make_adapter()

    adapter._instantiate_rails([_RailBuildInfo("_x_rail", lambda: object())], {})

    assert not any("agent rails built" in rec[0] for rec in server_log.info_records)
    assert any("agent rails built" in rec[0] for rec in server_log.debug_records)


def test_quiet_breakdown_still_reaches_the_agent_log_stream(loggers) -> None:
    """The sub-threshold breakdown is the one that explains un-slow time.

    Routing it to the module logger used to hide it entirely: that sink runs
    at INFO, so a DEBUG line there went nowhere.
    """
    server_log, module_log = loggers

    log = interface_deep._stage_breakdown_logger(1.0, 100.0)
    log("[AgentServer] probe")

    assert module_log.records == []
    assert len(server_log.debug_records) == 1


def test_threshold_override_forces_info(monkeypatch: pytest.MonkeyPatch, loggers) -> None:
    """Profiling runs need every breakdown, not just the slow ones."""
    server_log, _ = loggers
    monkeypatch.setenv(interface_deep._STAGE_LOG_THRESHOLD_ENV, "0")

    interface_deep._stage_breakdown_logger(0.1, 10_000.0)("[AgentServer] probe")

    assert len(server_log.info_records) == 1


def test_non_numeric_override_falls_back_to_the_site_threshold(
    monkeypatch: pytest.MonkeyPatch, loggers
) -> None:
    """A typo in the env var must not silently change reporting."""
    server_log, module_log = loggers
    monkeypatch.setenv(interface_deep._STAGE_LOG_THRESHOLD_ENV, "not-a-number")

    interface_deep._stage_breakdown_logger(500.0, 100.0)("[AgentServer] probe")

    assert len(server_log.info_records) == 1
    assert any("ignoring non-numeric" in rec[0] for rec in module_log.records)
