# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Persistent subagent runtime wiring (shipped template on; missing key off)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from jiuwenswarm.agents.harness.common.rails.browser_task_prompt_rail import (
    BrowserTaskPromptRail,
)
from jiuwenswarm.common.config import (
    is_subagent_runtime_enabled,
    subagent_runtime_limits,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
    _agent_ras_kwargs_from_config,
    _optional_enable_subagent_runtime,
)
from jiuwenswarm.server.runtime.agent_adapter.deep.pure_helpers import _session_has_live_subagent_runtime


def test_shipped_template_subagent_runtime_enabled_by_default() -> None:
    config_path = (
        Path(__file__).resolve().parents[3]
        / "jiuwenswarm"
        / "resources"
        / "config.yaml"
    )
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert config["react"]["subagent_runtime"]["enabled"] is True
    assert config["react"]["subagent_runtime"]["max_spawn_depth"] == 2
    assert config["react"]["subagent_runtime"]["max_concurrent_running"] == 20
    assert config["react"]["subagent_runtime"]["max_subagents"] == 20
    assert is_subagent_runtime_enabled(config) is True
    assert subagent_runtime_limits(config) == {
        "max_spawn_depth": 2,
        "max_concurrent_running": 20,
        "max_subagents": 20,
    }


def test_subagent_runtime_limits_defaults_and_rejects_bad_pairs() -> None:
    assert subagent_runtime_limits({}) == {
        "max_spawn_depth": 2,
        "max_concurrent_running": 20,
        "max_subagents": 20,
    }
    with pytest.raises(ValueError, match="max_spawn_depth"):
        subagent_runtime_limits({"react": {"subagent_runtime": {"max_spawn_depth": 0}}})
    with pytest.raises(ValueError, match="max_subagents"):
        subagent_runtime_limits(
            {"react": {"subagent_runtime": {"max_subagents": 1, "max_concurrent_running": 5}}}
        )
    with pytest.raises(ValueError, match="must be >= 1"):
        subagent_runtime_limits(
            {"react": {"subagent_runtime": {"max_concurrent_running": 0}}}
        )
    with pytest.raises(ValueError, match="must be an integer"):
        subagent_runtime_limits(
            {"react": {"subagent_runtime": {"max_concurrent_running": False}}}
        )
    with pytest.raises(ValueError, match="must be an integer"):
        subagent_runtime_limits(
            {"react": {"subagent_runtime": {"max_spawn_depth": 0.9}}}
        )
    numeric = subagent_runtime_limits(
        {"react": {"subagent_runtime": {"max_concurrent_running": "20", "max_subagents": "20"}}}
    )
    assert numeric["max_concurrent_running"] == 20
    assert numeric["max_subagents"] == 20


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
    assert _session_has_live_subagent_runtime(
        live, "sess-1"
    ) is True
    assert _session_has_live_subagent_runtime(
        idle, "sess-1"
    ) is False
    assert _session_has_live_subagent_runtime(
        SimpleNamespace(_instance=None), "sess-1"
    ) is False


def test_session_has_live_subagent_runtime_fail_closed_when_capacity_raises() -> None:
    def _boom() -> dict[str, int]:
        raise RuntimeError("capacity unavailable")

    occupied = SimpleNamespace(
        _instance=SimpleNamespace(
            _subagent_controls={
                "sess-1": SimpleNamespace(capacity=_boom),
            }
        )
    )
    assert _session_has_live_subagent_runtime(
        occupied, "sess-1"
    ) is True


def test_session_has_live_subagent_runtime_missing_controls_is_idle() -> None:
    """dest-stable SDK and never-spawned parents have no control map."""
    missing = SimpleNamespace(_instance=SimpleNamespace())
    invalid = SimpleNamespace(_instance=SimpleNamespace(_subagent_controls="bad"))
    assert _session_has_live_subagent_runtime(
        missing, "sess-1"
    ) is False
    assert _session_has_live_subagent_runtime(
        invalid, "sess-1"
    ) is False


def test_release_subagent_runtime_clears_progress_batch() -> None:
    from jiuwenswarm.server.runtime.agent_adapter.subagent_stream import (
        clear_all_subagent_progress_batches,
        resolve_subagent_parallel_fields,
    )

    clear_all_subagent_progress_batches()
    resolve_subagent_parallel_fields(
        parent_session_id="sess-1",
        subagent_id="sa-a",
        legacy_status="starting",
    )
    adapter = JiuWenSwarmDeepAdapter()
    adapter._instance = None
    adapter._is_session_scoped_adapter = True
    adapter._parent_session_id = "sess-1"

    import asyncio

    asyncio.run(adapter.release_subagent_runtime_for_session("sess-1"))
    again = resolve_subagent_parallel_fields(
        parent_session_id="sess-1",
        subagent_id="sa-b",
        legacy_status="starting",
    )
    assert again == (0, 1, False)


def test_agent_ras_passthrough_still_enabled() -> None:
    kwargs = _agent_ras_kwargs_from_config({"agent_ras": {"enabled": True}})
    assert "agent_ras" in kwargs
    assert kwargs["agent_ras"] is not False


def test_code_mode_create_instance_passes_agent_ras_kwargs() -> None:
    """code 模式独立调用 create_deep_agent，agent_ras 须与 deep/flash 路径对齐透传."""
    import inspect

    from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
        JiuwenSwarmCodeAdapter,
    )

    source = inspect.getsource(JiuwenSwarmCodeAdapter.create_instance)
    assert "**_agent_ras_kwargs_from_config(config_base)" in source
    assert "_bind_subagent_runtime_config(config_base)" in source


def test_bind_runtime_limits_uses_instance_config_and_survives_bad_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.common import config as config_mod

    monkeypatch.setattr(
        config_mod,
        "get_config",
        lambda: {
            "react": {
                "subagent_runtime": {
                    "max_spawn_depth": 1,
                    "max_concurrent_running": 4,
                    "max_subagents": 4,
                }
            }
        },
    )
    adapter = JiuWenSwarmDeepAdapter()
    adapter._instance = SimpleNamespace()
    adapter._bind_subagent_runtime_config(
        {
            "react": {
                "subagent_runtime": {
                    "max_spawn_depth": 2,
                    "max_concurrent_running": 20,
                    "max_subagents": 20,
                }
            }
        }
    )
    bound = adapter._instance.subagent_runtime_config
    assert bound.max_concurrent_running == 20
    assert bound.max_subagents == 20

    broken = JiuWenSwarmDeepAdapter()
    broken._instance = SimpleNamespace()
    broken._bind_subagent_runtime_config(
        {"react": {"subagent_runtime": {"max_spawn_depth": 0}}}
    )
    assert not hasattr(broken._instance, "subagent_runtime_config")


def test_bind_runtime_limits_warns_when_sdk_drops_a_key(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from dataclasses import dataclass

    import openjiuwen.harness.subagent_runtime.config as sdk_config

    @dataclass
    class _OldRuntimeConfig:
        max_subagents: int = 10
        max_concurrent_running: int = 10

    monkeypatch.setattr(sdk_config, "SubagentRuntimeConfig", _OldRuntimeConfig)
    adapter = JiuWenSwarmDeepAdapter()
    adapter._instance = SimpleNamespace()
    with caplog.at_level("WARNING"):
        adapter._bind_subagent_runtime_config(
            {
                "react": {
                    "subagent_runtime": {
                        "max_spawn_depth": 2,
                        "max_concurrent_running": 20,
                        "max_subagents": 20,
                    }
                }
            }
        )
    assert "max_spawn_depth" in caplog.text
    assert adapter._instance.subagent_runtime_config.max_concurrent_running == 20


def test_optional_enable_subagent_runtime_follows_deep_agent_config_signature() -> None:
    """Official pin omits the field; overlay / post-3A SDK keeps it."""
    from inspect import signature

    from openjiuwen.harness.schema.config import DeepAgentConfig

    got = _optional_enable_subagent_runtime(True)
    params = signature(DeepAgentConfig.__init__).parameters
    if "enable_subagent_runtime" in params:
        assert got == {"enable_subagent_runtime": True}
    else:
        assert got == {}


def test_optional_enable_subagent_runtime_omits_flag_on_legacy_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import dataclass

    from jiuwenswarm.server.runtime.agent_adapter import interface_deep as deep_mod
    from jiuwenswarm.server.runtime.agent_adapter.deep import agent_config_helpers as cfg_mod

    @dataclass
    class _LegacyDeepAgentConfig:
        enable_task_loop: bool = False

    # 定义在 agent_config_helpers；facade re-export 不会改函数体内名字查找。
    monkeypatch.setattr(cfg_mod, "DeepAgentConfig", _LegacyDeepAgentConfig)
    monkeypatch.setattr(deep_mod, "DeepAgentConfig", _LegacyDeepAgentConfig)
    assert deep_mod._optional_enable_subagent_runtime(True) == {}
