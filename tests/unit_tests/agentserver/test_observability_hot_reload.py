# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Mixed-entry hot reload with real SDK, file exporter and SQLite writes."""

from __future__ import annotations

import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest
from openjiuwen.extensions.observability import demand, setup
from openjiuwen.extensions.observability.config import ObservabilityConfig
from openjiuwen.harness.observability import close_agent_run_span, open_agent_run_span
from openjiuwen.harness.observability import setup as agent_setup

from jiuwenswarm.agents.harness import agent_observability, observability_runtime
from jiuwenswarm.agents.harness.team import team_manager
from jiuwenswarm.observability import runtime as trajectory_runtime
from jiuwenswarm.observability.config import session_database_path


@pytest.fixture
def hot_reload_environment(monkeypatch, tmp_path):
    trajectory_runtime.shutdown_trajectory_runtime()
    setup.shutdown_observability()
    demand.reset_observability_demands()
    monkeypatch.setattr(observability_runtime, "_export_overrides", {})
    monkeypatch.setattr(agent_observability, "_agent_observability_active", False)
    monkeypatch.setattr(agent_observability, "_force_ever_enabled", False)
    monkeypatch.setattr(agent_observability, "_plugin_trajectory_demands", set())
    monkeypatch.setattr(team_manager, "_observability_active", False)
    monkeypatch.setattr(agent_setup, "_tracing_enabled", False)
    config = {
        "observability": {
            "enabled": True,
            "exporter": "file",
            "traces_dir": str(tmp_path / "export"),
        },
        "trajectory_ui": {
            "enabled": True,
            "diagnosis_enabled": False,
            "db_path": str(tmp_path / "ui"),
            "flush_interval_ms": 10,
        },
        "react": {"evolution": {"skill_evolution": False}},
    }
    monkeypatch.setattr(agent_observability, "get_config", lambda: config)
    monkeypatch.setattr(team_manager, "get_config", lambda: config)
    entries = {
        "agent": agent_observability.sync_agent_observability,
        # Code uses the same Agent lifecycle; it is not another demand owner.
        "code": agent_observability.sync_agent_observability,
        "team": team_manager.sync_team_observability,
    }

    def emit(label, *, exported, stored, mode="agent.fast"):
        sid = f"hot-reload-{label}"
        span = open_agent_run_span(session_id=sid, mode=mode)
        if span is None:
            # Probe the idle owner's live provider even after Agent released.
            span = setup.get_tracer(__name__).start_span(
                f"agent.{sid}",
                attributes={
                    "gen_ai.conversation.id": sid,
                    "gen_ai.operation.name": "invoke_agent",
                },
            )
            span.end()
        else:
            close_agent_run_span(span, session_id=sid, output="OK")
        setup.force_flush_provider()
        lines = [
            line
            for path in (tmp_path / "export").glob("*.jsonl")
            for line in path.read_text(encoding="utf-8").splitlines()
            if sid in line
        ]
        assert bool(lines) is exported, (label, lines)
        db = session_database_path(tmp_path / "ui", sid)
        deadline = time.monotonic() + 3
        count = 0
        while stored and time.monotonic() < deadline:
            if db.exists():
                with sqlite3.connect(db) as connection:
                    if connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE name='otlp_span_records'"
                    ).fetchone():
                        count = connection.execute(
                            "SELECT count(*) FROM otlp_span_records"
                        ).fetchone()[0]
                if count:
                    break
            time.sleep(0.01)
        if stored:
            assert count > 0, label
        else:
            assert trajectory_runtime.get_trajectory_runtime_sink() is None
            assert not db.exists(), label
        return span

    yield SimpleNamespace(config=config, entries=entries, emit=emit, root=tmp_path)
    agent_observability.shutdown_agent_observability()
    team_manager.shutdown_team_observability()
    trajectory_runtime.shutdown_trajectory_runtime()
    setup.shutdown_observability()
    demand.reset_observability_demands()


@pytest.mark.parametrize("refresh", ["agent", "team", "code"])
@pytest.mark.parametrize("evolution", [False, True])
def test_one_entry_refreshes_both_export_and_ui_policies(
    hot_reload_environment, refresh, evolution,
):
    env = hot_reload_environment
    env.config["react"]["evolution"]["skill_evolution"] = evolution
    env.entries["team"]()
    env.entries["agent"]()
    mode = "agent.code.normal" if refresh == "code" else "agent.fast"
    env.emit("both-on", exported=True, stored=True, mode=mode)

    env.config["observability"]["enabled"] = False
    env.entries[refresh]()
    env.emit("export-off-ui-on", exported=False, stored=True, mode=mode)

    env.config["observability"]["enabled"] = True
    env.config["trajectory_ui"]["enabled"] = False
    env.entries[refresh]()
    env.emit("export-on-ui-off", exported=True, stored=False, mode=mode)

    env.config["observability"]["enabled"] = False
    env.entries[refresh]()
    processor = demand.get_trajectory_span_processor()
    subscription = processor.subscribe(include_span_categories={"agent"})
    try:
        env.emit("both-off", exported=False, stored=False, mode=mode)
        if evolution:
            trajectory, issues = processor.drain(subscription)
            assert trajectory is not None
            assert not issues
    finally:
        processor.unsubscribe(subscription)

    env.config["trajectory_ui"]["enabled"] = True
    env.entries[refresh]()
    env.emit("ui-reenabled", exported=False, stored=True, mode=mode)
    env.config["observability"]["enabled"] = True
    env.entries[refresh]()
    env.emit("both-reenabled", exported=True, stored=True, mode=mode)


def test_team_refresh_preserves_debug_export_override(hot_reload_environment):
    env = hot_reload_environment
    agent_observability.sync_agent_observability(force=True)
    env.entries["team"]()
    env.config["observability"]["enabled"] = False
    env.entries["team"]()
    env.emit("debug-override", exported=True, stored=True)


@pytest.mark.parametrize("entry", ["agent", "team"])
@pytest.mark.parametrize("section", ["missing", None, False])
def test_unconfigured_observability_does_not_create_exporter(
    hot_reload_environment, monkeypatch, entry, section,
):
    from openjiuwen.extensions.observability import runtime as sdk_runtime

    env = hot_reload_environment
    env.config.pop("observability")
    if section != "missing":
        env.config["observability"] = section
    env.config["trajectory_ui"]["enabled"] = False
    created = []
    monkeypatch.setattr(sdk_runtime, "build_span_exporter", lambda config: created.append(config))

    env.entries[entry]()

    assert not created
    assert not setup.is_initialized()
    assert observability_runtime._export_overrides == {}


@pytest.mark.parametrize("entry", ["agent", "team"])
@pytest.mark.parametrize("capture", ["ui", "evolution"])
def test_missing_observability_preserves_capture_without_export(
    hot_reload_environment, monkeypatch, entry, capture,
):
    from openjiuwen.extensions.observability import runtime as sdk_runtime
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    env = hot_reload_environment
    env.config.pop("observability")
    env.config["trajectory_ui"]["enabled"] = capture == "ui"
    env.config["react"]["evolution"]["skill_evolution"] = capture == "evolution"
    exporter = InMemorySpanExporter()
    # Replace only the transport; the real provider and config-exporter gate run.
    monkeypatch.setattr(sdk_runtime, "build_span_exporter", lambda config: exporter)

    env.entries[entry]()
    assert setup.is_initialized()
    processor = demand.get_trajectory_span_processor()
    subscription = processor.subscribe(include_span_categories={"agent"})
    try:
        env.emit("agent.unconfigured-" + capture, exported=False, stored=capture == "ui")
        trajectory, issues = processor.drain(subscription)
        assert trajectory is not None
        assert not issues
        assert not exporter.get_finished_spans()
    finally:
        processor.unsubscribe(subscription)


def test_missing_observability_still_allows_explicit_debug_export(
    hot_reload_environment, monkeypatch,
):
    from openjiuwen.extensions.observability import runtime as sdk_runtime
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    env = hot_reload_environment
    env.config.pop("observability")
    exporter = InMemorySpanExporter()
    monkeypatch.setattr(sdk_runtime, "build_span_exporter", lambda config: exporter)

    agent_observability.sync_agent_observability(force=True)
    env.emit("unconfigured-debug", exported=False, stored=True)

    assert exporter.get_finished_spans()


def test_capture_policy_preserves_external_provider(hot_reload_environment):
    env = hot_reload_environment
    setup.init_observability(ObservabilityConfig(
        enabled=True, exporter="file", traces_dir=str(env.root / "export"),
        global_instrument_enable=False,
    ))
    env.config["observability"]["enabled"] = False
    env.entries["team"]()
    env.entries["agent"]()
    env.emit("external-owner", exported=True, stored=True)
    agent_observability.shutdown_agent_observability()
    team_manager.shutdown_team_observability()
    assert setup.is_initialized()


@pytest.mark.parametrize("departing", ["agent", "team"])
def test_owner_shutdown_preserves_other_owner_and_plugin_capture(hot_reload_environment, departing):
    env = hot_reload_environment
    env.entries["team"]()
    env.entries["agent"]()
    if departing == "agent":
        agent_observability.shutdown_agent_observability()
    else:
        team_manager.shutdown_team_observability()
    env.emit("after-owner-exit", exported=True, stored=True)
    env.config["observability"]["enabled"] = False
    env.config["trajectory_ui"]["enabled"] = False
    lease = agent_observability.acquire_plugin_trajectory_demand("plugin-session", "test")
    try:
        env.emit("plugin-only", exported=False, stored=False)
        assert setup.is_initialized()
    finally:
        lease.release()
        team_manager.shutdown_team_observability()
    assert not setup.is_initialized()
    assert observability_runtime._export_overrides == {}


@pytest.mark.parametrize("refresh", ["agent", "team", "code"])
@pytest.mark.parametrize("evolution", [False, True])
def test_turning_both_switches_off_clears_idle_entry_policy(
    hot_reload_environment, refresh, evolution,
):
    env = hot_reload_environment
    env.config["react"]["evolution"]["skill_evolution"] = evolution
    env.entries["agent"]()
    env.entries["team"]()
    env.emit("before-direct-disable", exported=True, stored=True)
    env.config["observability"]["enabled"] = False
    env.config["trajectory_ui"]["enabled"] = False
    env.entries[refresh]()
    env.emit("after-direct-disable", exported=False, stored=False)


def test_failed_acquire_does_not_register_policy_owner(hot_reload_environment, monkeypatch):
    env = hot_reload_environment

    def fail(_config, *, export):
        raise RuntimeError("injected acquire failure")

    monkeypatch.setattr(agent_observability, "acquire_observability", fail)
    env.entries["agent"]()
    assert observability_runtime._export_overrides == {}
    env.config["observability"]["enabled"] = False
    env.entries["team"]()
    env.emit("after-failed-agent", exported=False, stored=True)


def test_concurrent_entries_read_config_in_lifecycle_order(hot_reload_environment, monkeypatch):
    env = hot_reload_environment
    acquiring = threading.Event()
    finish_acquire = threading.Event()
    team_started = threading.Event()
    team_read_config = threading.Event()
    acquired = agent_observability.acquire_observability
    errors = []

    def pause_acquire(config, *, export):
        acquiring.set()
        assert finish_acquire.wait(5)
        return acquired(config, export=export)

    def team_config():
        team_read_config.set()
        return env.config

    def run_agent():
        try:
            env.entries["agent"]()
        except BaseException as exc:
            errors.append(exc)

    def run_team():
        team_started.set()
        try:
            env.entries["team"]()
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(agent_observability, "acquire_observability", pause_acquire)
    monkeypatch.setattr(team_manager, "get_config", team_config)
    agent_thread = threading.Thread(target=run_agent, daemon=True)
    team_thread = threading.Thread(target=run_team, daemon=True)
    agent_thread.start()
    try:
        assert acquiring.wait(5)
        env.config["observability"]["enabled"] = False
        team_thread.start()
        assert team_started.wait(5)
        # Team must read the new config after Agent commits its old demand.
        assert not team_read_config.wait(0.05)
    finally:
        finish_acquire.set()
        agent_thread.join(5)
        if team_thread.ident is not None:
            team_thread.join(5)
    assert not agent_thread.is_alive()
    assert not team_thread.is_alive()
    assert not errors
    env.emit("concurrent-off", exported=False, stored=True)
