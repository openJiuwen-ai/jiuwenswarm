# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression tests for process-level trajectory runtime wiring."""

import pytest
import openjiuwen.agent_teams.observability as observability

from jiuwenswarm.agents.harness import agent_observability
from jiuwenswarm.agents.harness import observability_runtime
from jiuwenswarm.agents.harness.team import team_manager


@pytest.fixture(autouse=True)
def reset_observability_demands():
    observability_runtime.reset_observability_demands()
    agent_observability._agent_observability_active = False
    agent_observability._force_ever_enabled = False
    agent_observability._ROOT_SPANS.clear()
    team_manager._observability_active = False
    yield
    observability_runtime.reset_observability_demands()
    agent_observability._agent_observability_active = False
    agent_observability._force_ever_enabled = False
    agent_observability._ROOT_SPANS.clear()
    team_manager._observability_active = False


def test_trajectory_processor_is_shared_across_runtimes(monkeypatch):
    processor = object()
    monkeypatch.setattr(
        observability_runtime, "_TRAJECTORY_SPAN_PROCESSOR", processor
    )

    first = observability_runtime.get_trajectory_span_processor()
    second = observability_runtime.get_trajectory_span_processor()

    assert first is processor
    assert second is first


def test_agent_evolution_enables_observability_without_manual_switch(monkeypatch):
    requests = []
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
        "acquire_observability_demand",
        lambda runtime, **_kwargs: requests.append(runtime),
    )

    agent_observability.sync_agent_observability()

    assert requests == ["agent"]
    assert agent_observability._agent_observability_active is True


def test_team_evolution_enables_observability_without_manual_switch(monkeypatch):
    requests = []
    monkeypatch.setattr(
        team_manager,
        "get_config",
        lambda: {
            "react": {"evolution": {"skill_evolution": True}},
            "team_observability": {"enabled": False},
        },
    )
    monkeypatch.setattr(
        team_manager,
        "acquire_observability_demand",
        lambda runtime, **_kwargs: requests.append(runtime),
    )

    team_manager.sync_team_observability()

    assert requests == ["team"]
    assert team_manager._observability_active is True


def test_agent_observability_init_receives_process_processor(monkeypatch, tmp_path):
    processor = object()
    calls = []
    monkeypatch.setattr(
        observability_runtime, "_TRAJECTORY_SPAN_PROCESSOR", processor
    )
    monkeypatch.setattr(agent_observability, "_agent_observability_active", False)
    monkeypatch.setattr(agent_observability, "get_user_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(
        agent_observability,
        "get_config",
        lambda: {
            "react": {"evolution": {"skill_evolution": False}},
            "agent_observability": {"enabled": True},
        },
    )
    state = {"initialized": False}
    monkeypatch.setattr(observability, "is_initialized", lambda: state["initialized"])
    monkeypatch.setattr(
        observability,
        "init_observability",
        lambda config, **kwargs: (
            calls.append((config, kwargs)),
            state.__setitem__("initialized", True),
        ),
    )

    agent_observability.sync_agent_observability()

    assert calls[0][1]["additional_span_processors"] == (processor,)


def test_team_observability_init_receives_process_processor(monkeypatch, tmp_path):
    processor = object()
    calls = []
    monkeypatch.setattr(
        observability_runtime, "_TRAJECTORY_SPAN_PROCESSOR", processor
    )
    monkeypatch.setattr(team_manager, "_observability_active", False)
    monkeypatch.setattr(team_manager, "get_user_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(
        team_manager,
        "get_config",
        lambda: {
            "react": {"evolution": {"skill_evolution": False}},
            "team_observability": {"enabled": True},
        },
    )
    state = {"initialized": False}
    monkeypatch.setattr(observability, "is_initialized", lambda: state["initialized"])
    monkeypatch.setattr(
        observability,
        "init_observability",
        lambda config, **kwargs: (
            calls.append((config, kwargs)),
            state.__setitem__("initialized", True),
        ),
    )

    team_manager.sync_team_observability()

    assert calls[0][1]["additional_span_processors"] == (processor,)


def test_team_observability_can_be_disabled_without_evolution_demand(monkeypatch):
    releases = []
    team_manager._observability_active = True
    monkeypatch.setattr(
        team_manager,
        "release_observability_demand",
        lambda runtime: releases.append(runtime),
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


def test_shared_observability_demand_keeps_provider_until_last_runtime_releases(monkeypatch):
    state = {"initialized": False}
    shutdowns = []

    monkeypatch.setattr(observability, "is_initialized", lambda: state["initialized"])

    def _init(_config, **_kwargs):
        state["initialized"] = True

    monkeypatch.setattr(observability, "init_observability", _init)
    monkeypatch.setattr(
        observability,
        "shutdown_observability",
        lambda: (shutdowns.append(True), state.__setitem__("initialized", False)),
    )

    config = object()
    observability_runtime.acquire_observability_demand(
        "agent",
        observability_config=config,
    )
    observability_runtime.acquire_observability_demand(
        "team",
        observability_config=config,
    )

    observability_runtime.release_observability_demand("agent")
    assert state["initialized"] is True
    assert shutdowns == []
    observability_runtime.release_observability_demand("team")
    assert state["initialized"] is False
    assert shutdowns == [True]


def test_shared_observability_demand_does_not_shutdown_external_provider(monkeypatch):
    shutdowns = []
    monkeypatch.setattr(observability, "is_initialized", lambda: True)
    monkeypatch.setattr(observability, "init_observability", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(observability, "shutdown_observability", lambda: shutdowns.append(True))

    observability_runtime.acquire_observability_demand(
        "agent",
        observability_config=object(),
    )
    observability_runtime.release_observability_demand("agent")

    assert shutdowns == []
