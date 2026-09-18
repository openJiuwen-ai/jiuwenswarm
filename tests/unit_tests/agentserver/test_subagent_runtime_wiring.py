# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Persistent subagent runtime wiring on dest-stable (default off)."""

from __future__ import annotations

from types import SimpleNamespace

from jiuwenswarm.agents.harness.common.rails.browser_task_prompt_rail import (
    BrowserTaskPromptRail,
)
from jiuwenswarm.common.config import is_subagent_runtime_enabled
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
    _agent_ras_kwargs_from_config,
)


def test_is_subagent_runtime_enabled_defaults_off() -> None:
    assert is_subagent_runtime_enabled({}) is False
    assert is_subagent_runtime_enabled({"react": {}}) is False
    assert is_subagent_runtime_enabled({"react": {"subagent_runtime": {}}}) is False
    assert is_subagent_runtime_enabled(
        {"react": {"subagent_runtime": {"enabled": False}}}
    ) is False
    assert is_subagent_runtime_enabled(
        {"react": {"subagent_runtime": {"enabled": True}}}
    ) is True


def test_resolve_enable_subagent_runtime_defaults_off() -> None:
    adapter = JiuWenSwarmDeepAdapter()
    assert adapter._resolve_enable_subagent_runtime({}) is False
    assert adapter._resolve_enable_subagent_runtime(
        {"react": {"subagent_runtime": {"enabled": True}}}
    ) is True


def test_build_subagent_rail_uses_browser_policy_and_stays_off() -> None:
    adapter = JiuWenSwarmDeepAdapter()
    rail = adapter._build_subagent_rail({})
    assert isinstance(rail, BrowserTaskPromptRail)
    assert rail.enable_subagent_runtime is False


def test_session_has_live_subagent_runtime_reads_capacity() -> None:
    live = SimpleNamespace(
        _instance=SimpleNamespace(
            _subagent_controls={
                "sess-1": SimpleNamespace(capacity=lambda: {"used": 2}),
            }
        )
    )
    idle = SimpleNamespace(
        _instance=SimpleNamespace(
            _subagent_controls={
                "sess-1": SimpleNamespace(capacity=lambda: {"used": 0}),
            }
        )
    )
    assert JiuWenSwarmDeepAdapter._session_has_live_subagent_runtime(
        live, "sess-1"
    ) is True
    assert JiuWenSwarmDeepAdapter._session_has_live_subagent_runtime(
        idle, "sess-1"
    ) is False
    assert JiuWenSwarmDeepAdapter._session_has_live_subagent_runtime(
        SimpleNamespace(_instance=None), "sess-1"
    ) is False


def test_agent_ras_passthrough_still_enabled() -> None:
    kwargs = _agent_ras_kwargs_from_config({"agent_ras": {"enabled": True}})
    assert "agent_ras" in kwargs
    assert kwargs["agent_ras"] is not False
