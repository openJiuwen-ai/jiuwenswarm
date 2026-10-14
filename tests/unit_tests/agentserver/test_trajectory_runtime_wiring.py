# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression tests for process-level trajectory runtime wiring.

Provider demand coordination itself lives in the SDK
(``openjiuwen.extensions.observability.demand``) and is covered there. What is
tested here is this platform's half: the config gate that decides when each
runtime asks for the provider, and that the shared trajectory processor still
reaches the exporter pipeline through that path.
"""

import pytest
from typing import Any

from openjiuwen.agent_teams.observability import setup as team_observability_setup
from openjiuwen.extensions.observability import demand as observability_demand
from openjiuwen.extensions.observability import setup as shared_observability_setup
from openjiuwen.harness.observability import setup as agent_observability_setup
from openjiuwen.harness.observability import span_context as agent_span_context

from jiuwenswarm.agents.harness import agent_observability
from jiuwenswarm.agents.harness.team import team_manager
from jiuwenswarm.observability import runtime as trajectory_runtime


@pytest.fixture(autouse=True)
def reset_observability_demands():
    """Isolate the process-wide observability state around each test."""
    def _reset():
        trajectory_runtime.shutdown_trajectory_runtime()
        observability_demand.reset_observability_demands()
        agent_span_context.reset_run_root_spans()
        agent_observability._agent_observability_active = False
        agent_observability._force_ever_enabled = False
        agent_observability._file_exporter_fanout = None
        team_manager._observability_active = False

    _reset()
    yield
    _reset()


def test_agent_evolution_enables_observability_without_manual_switch(monkeypatch):
    acquired = []
    monkeypatch.setattr(
        agent_observability,
        "get_config",
        lambda: {
            "react": {"evolution": {"skill_evolution": True}},
            "agent_observability": {"enabled": False},
        },
    )
    monkeypatch.setattr(
        agent_observability,
        "acquire_observability",
        lambda config: acquired.append(config) or False,
    )

    agent_observability.sync_agent_observability()

    assert len(acquired) == 1
    assert agent_observability._agent_observability_active is True


def test_team_evolution_enables_observability_without_manual_switch(monkeypatch):
    acquired = []
    monkeypatch.setattr(
        team_manager,
        "get_config",
        lambda: {
            "react": {"evolution": {"skill_evolution": True}},
            "team_observability": {"enabled": False},
        },
    )
    monkeypatch.setattr(
        team_manager.team_observability,
        "acquire_observability",
        lambda config: acquired.append(config) or False,
    )

    team_manager.sync_team_observability()

    assert len(acquired) == 1
    assert team_manager._observability_active is True


def test_team_observability_can_be_disabled_without_evolution_demand(monkeypatch):
    releases = []
    team_manager._observability_active = True
    monkeypatch.setattr(
        team_manager.team_observability,
        "release_observability",
        lambda: releases.append("team"),
    )
    monkeypatch.setattr(
        team_manager,
        "get_config",
        lambda: {
            "react": {"evolution": {"skill_evolution": False}},
            "team_observability": {"enabled": False},
        },
    )

    team_manager.sync_team_observability()

    assert releases == ["team"]


def test_trajectory_ui_is_an_additive_agent_provider_demand(monkeypatch, tmp_path):
    acquired = []
    runtime_settings = []
    monkeypatch.setattr(
        agent_observability,
        "get_config",
        lambda: {
            "agent_observability": {"enabled": False, "exporter": "file"},
            "trajectory_ui": {
                "enabled": True,
                "db_path": str(tmp_path / "trajectory.sqlite3"),
            },
        },
    )
    monkeypatch.setattr(
        agent_observability,
        "acquire_observability",
        lambda config: acquired.append(config) or False,
    )
    monkeypatch.setattr(
        agent_observability,
        "sync_trajectory_runtime",
        lambda settings, **kwargs: runtime_settings.append((settings, kwargs)),
    )

    agent_observability.sync_agent_observability()

    assert len(acquired) == 1
    assert len(runtime_settings) == 1
    assert runtime_settings[0][0].enabled is True
    assert runtime_settings[0][0].database_path == tmp_path / "trajectory.sqlite3"
    assert runtime_settings[0][1] == {"demand": "agent"}
    assert agent_observability._agent_observability_active is True


def test_disabled_trajectory_runtime_is_stopped_while_exporters_remain_active(
    monkeypatch,
):
    runtime_settings = []
    monkeypatch.setattr(
        agent_observability,
        "get_config",
        lambda: {
            "agent_observability": {"enabled": True},
            "trajectory_ui": {"enabled": False},
        },
    )
    monkeypatch.setattr(
        agent_observability,
        "acquire_observability",
        lambda _config: False,
    )
    monkeypatch.setattr(
        agent_observability,
        "sync_trajectory_runtime",
        lambda settings, **kwargs: runtime_settings.append((settings, kwargs)),
    )

    agent_observability.sync_agent_observability()

    assert len(runtime_settings) == 1
    assert runtime_settings[0][0].enabled is False
    assert runtime_settings[0][1] == {"demand": "agent"}
    assert agent_observability._agent_observability_active is True


def test_agent_shutdown_unregisters_trajectory_before_releasing_provider(monkeypatch):
    events = []
    agent_observability._agent_observability_active = True
    monkeypatch.setattr(
        agent_observability,
        "shutdown_trajectory_runtime",
        lambda **kwargs: events.append("trajectory.shutdown") or True,
    )
    monkeypatch.setattr(
        agent_observability,
        "release_observability",
        lambda: events.append("provider.release"),
    )

    agent_observability.shutdown_agent_observability()

    assert events == ["trajectory.shutdown", "provider.release"]
    assert agent_observability._agent_observability_active is False


def test_agent_observability_init_receives_process_processor(monkeypatch, tmp_path):
    """Evolution captures trajectories off the spans this processor sees."""
    processor = object()
    span_record_processor = object()
    calls = []
    state = {"initialized": False}
    monkeypatch.setattr(
        observability_demand, "get_trajectory_span_processor", lambda: processor
    )
    monkeypatch.setattr(
        observability_demand,
        "get_span_record_processor",
        lambda: span_record_processor,
    )
    monkeypatch.setattr(
        shared_observability_setup, "is_initialized", lambda: state["initialized"]
    )
    monkeypatch.setattr(
        agent_observability_setup,
        "init_shared_observability",
        lambda config, additional_span_processors=(): (
            calls.append(additional_span_processors),
            state.__setitem__("initialized", True),
        ),
    )
    monkeypatch.setattr(agent_observability, "get_user_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(
        agent_observability,
        "get_config",
        lambda: {
            "react": {"evolution": {"skill_evolution": False}},
            "agent_observability": {"enabled": True},
        },
    )

    agent_observability.sync_agent_observability()

    assert calls == [(processor, span_record_processor)]


def test_team_observability_init_receives_process_processor(monkeypatch, tmp_path):
    processor = object()
    span_record_processor = object()
    calls = []
    state = {"initialized": False}
    monkeypatch.setattr(
        observability_demand, "get_trajectory_span_processor", lambda: processor
    )
    monkeypatch.setattr(
        observability_demand,
        "get_span_record_processor",
        lambda: span_record_processor,
    )
    monkeypatch.setattr(
        shared_observability_setup, "is_initialized", lambda: state["initialized"]
    )
    monkeypatch.setattr(
        team_observability_setup,
        "init_observability",
        lambda config, additional_span_processors=(): (
            calls.append(additional_span_processors),
            state.__setitem__("initialized", True),
        ),
    )
    monkeypatch.setattr(team_manager, "get_user_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(
        team_manager,
        "get_config",
        lambda: {
            "react": {"evolution": {"skill_evolution": False}},
            "team_observability": {"enabled": True},
        },
    )

    team_manager.sync_team_observability()

    assert calls == [(processor, span_record_processor)]


def test_team_trajectory_ui_starts_sink_and_acquires_provider(monkeypatch, tmp_path):
    """trajectory_ui.enabled pulls the Team provider up and starts the sink."""
    acquired = []
    runtime_settings = []
    monkeypatch.setattr(
        team_manager,
        "get_config",
        lambda: {
            "team_observability": {"enabled": False, "exporter": "file"},
            "trajectory_ui": {
                "enabled": True,
                "db_path": str(tmp_path / "trajectory.sqlite3"),
            },
        },
    )
    monkeypatch.setattr(
        team_manager.team_observability,
        "acquire_observability",
        lambda config: acquired.append(config) or False,
    )
    monkeypatch.setattr(
        team_manager,
        "sync_trajectory_runtime",
        lambda settings, **kwargs: runtime_settings.append((settings, kwargs)),
    )

    team_manager.sync_team_observability()

    assert len(acquired) == 1
    assert len(runtime_settings) == 1
    assert runtime_settings[0][0].enabled is True
    assert runtime_settings[0][0].database_path == tmp_path / "trajectory.sqlite3"
    assert runtime_settings[0][1] == {"demand": "team"}
    assert team_manager._observability_active is True


def test_team_trajectory_disabled_stops_sink(monkeypatch, tmp_path):
    """Team trajectory disabled stops the sink without releasing the provider."""
    runtime_settings = []
    team_manager._observability_active = True
    monkeypatch.setattr(
        team_manager,
        "get_config",
        lambda: {
            "react": {"evolution": {"skill_evolution": False}},
            "team_observability": {"enabled": True, "exporter": "file"},
            "trajectory_ui": {"enabled": False},
        },
    )
    monkeypatch.setattr(
        team_manager,
        "sync_trajectory_runtime",
        lambda settings, **kwargs: runtime_settings.append((settings, kwargs)),
    )
    monkeypatch.setattr(
        team_manager.team_observability,
        "acquire_observability",
        lambda config: False,
    )

    team_manager.sync_team_observability()

    assert len(runtime_settings) == 1
    assert runtime_settings[0][0].enabled is False
    assert runtime_settings[0][1] == {"demand": "team"}
    assert team_manager._observability_active is True


class _FakeInnerProcessor:
    """Stand-in for the BatchSpanProcessor wrapped by the fan-out adapter."""

    def __init__(self, *, fail: bool = False) -> None:
        self.on_end_calls = 0
        self.shutdown_calls = 0
        self._fail = fail

    def on_start(self, span: Any, parent_context: Any = None) -> None:
        pass

    def on_end(self, span: Any) -> None:
        self.on_end_calls += 1

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True

    def shutdown(self) -> None:
        self.shutdown_calls += 1
        if self._fail:
            raise RuntimeError("fan-out shutdown failed")


class _BrokenFanout:
    """A fan-out holder whose shutdown() raises before any cleanup."""

    def __init__(self) -> None:
        self.shutdown_calls = 0

    def shutdown(self) -> None:
        self.shutdown_calls += 1
        raise RuntimeError("fan-out shutdown failed")


def _make_real_adapter(*, fail: bool = False):
    """Real SafeSpanProcessor wrapping a fake inner processor (review CR-002:
    the object registered on the provider is the ADAPTER; the shutdown gate
    lives on the adapter, not the inner processor)."""
    from openjiuwen.extensions.observability.runtime import SafeSpanProcessor

    inner = _FakeInnerProcessor(fail=fail)
    return SafeSpanProcessor(inner), inner


def test_shutdown_shuts_down_and_gates_the_fanout_adapter(monkeypatch):
    """Regression (PR #7971 review CR-001 + CR-002): shutdown must reach the
    SafeSpanProcessor adapter so its _shutdown_called latch flips — later
    on_end calls short-circuit before the inner processor — and the reference
    is cleared only on success."""
    adapter, inner = _make_real_adapter()
    monkeypatch.setattr(
        agent_observability, "shutdown_trajectory_runtime", lambda **kwargs: True
    )
    monkeypatch.setattr(agent_observability, "release_observability", lambda: None)
    agent_observability._agent_observability_active = True
    agent_observability._file_exporter_fanout = adapter

    agent_observability.shutdown_agent_observability()

    assert inner.shutdown_calls == 1
    assert agent_observability._file_exporter_fanout is None
    assert agent_observability._agent_observability_active is False
    # Adapter short-circuit: spans ending after shutdown never reach the inner.
    adapter.on_end(object())
    assert inner.on_end_calls == 0


def test_shutdown_fanout_not_gated_by_active_flag(monkeypatch):
    """An orphaned fan-out adapter (state inconsistency) must still be shut
    down — the cleanup sits before the ``_agent_observability_active`` gate."""
    adapter, inner = _make_real_adapter()
    monkeypatch.setattr(
        agent_observability, "shutdown_trajectory_runtime", lambda **kwargs: True
    )
    agent_observability._agent_observability_active = False
    agent_observability._file_exporter_fanout = adapter

    agent_observability.shutdown_agent_observability()

    assert inner.shutdown_calls == 1
    assert agent_observability._file_exporter_fanout is None


def test_fanout_inner_shutdown_failure_keeps_reference_and_blocks_spans(monkeypatch):
    """The SafeSpanProcessor adapter latches _shutdown_called before the inner
    shutdown runs, so an inner failure still gates on_end — and shutdown()
    itself does not raise (failures are logged inside the adapter)."""
    adapter, inner = _make_real_adapter(fail=True)
    monkeypatch.setattr(
        agent_observability, "shutdown_trajectory_runtime", lambda **kwargs: True
    )
    monkeypatch.setattr(agent_observability, "release_observability", lambda: None)
    agent_observability._agent_observability_active = True
    agent_observability._file_exporter_fanout = adapter

    agent_observability.shutdown_agent_observability()

    assert inner.shutdown_calls == 1
    # Adapter latch flipped despite the inner failure: no further spans pass.
    adapter.on_end(object())
    assert inner.on_end_calls == 0
    # Adapter.shutdown() swallowed the failure, so the reference cleared normally.
    assert agent_observability._file_exporter_fanout is None
    assert agent_observability._agent_observability_active is False


def test_fanout_holder_shutdown_failure_keeps_reference_and_release(monkeypatch):
    """Defensive path (holder is not the adapter): a raising shutdown keeps
    the reference so a later attach stays a no-op instead of stacking a second
    processor, and the shared provider release still proceeds."""
    broken = _BrokenFanout()
    released = []
    monkeypatch.setattr(
        agent_observability, "shutdown_trajectory_runtime", lambda **kwargs: True
    )
    monkeypatch.setattr(
        agent_observability, "release_observability", lambda: released.append(True)
    )
    agent_observability._agent_observability_active = True
    agent_observability._file_exporter_fanout = broken

    agent_observability.shutdown_agent_observability()

    assert broken.shutdown_calls == 1
    assert agent_observability._file_exporter_fanout is broken
    assert released == [True]
    assert agent_observability._agent_observability_active is False


def test_attach_registers_the_safe_span_processor_adapter(monkeypatch, tmp_path):
    """_attach_file_exporter_fanout must register (and hold) the
    SafeSpanProcessor adapter on the global tracer provider — the same shape
    runtime.add_span_processors would register — never the bare inner
    BatchSpanProcessor (review CR-002)."""
    from jiuwenswarm.agents.harness import agent_observability as mod
    from openjiuwen.extensions.observability import runtime as obs_runtime
    from openjiuwen.extensions.observability import setup as obs_setup
    from openjiuwen.extensions.observability.file_exporter import TraceFileExporter

    registered = []

    class FakeProvider:
        def add_span_processor(self, processor):
            registered.append(processor)

    monkeypatch.setattr(obs_setup, "is_initialized", lambda: True)
    monkeypatch.setattr(
        "opentelemetry.trace.get_tracer_provider", lambda: FakeProvider()
    )
    monkeypatch.setattr(
        TraceFileExporter, "__init__",
        lambda self, root_dir, retention_days=7: None,
    )

    mod._file_exporter_fanout = None
    try:
        mod._attach_file_exporter_fanout(str(tmp_path / ".trace"), {})

        assert len(registered) == 1
        assert isinstance(registered[0], obs_runtime.SafeSpanProcessor)
        assert mod._file_exporter_fanout is registered[0]
        # Idempotent while attached: a second call must not register another.
        mod._attach_file_exporter_fanout(str(tmp_path / ".trace"), {})
        assert len(registered) == 1
    finally:
        holder = mod._file_exporter_fanout
        mod._file_exporter_fanout = None
        if holder is not None:
            holder.shutdown()
