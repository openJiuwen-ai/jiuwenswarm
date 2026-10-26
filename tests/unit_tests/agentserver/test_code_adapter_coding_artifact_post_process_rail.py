# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from unittest.mock import MagicMock

import openjiuwen.agent_evolving.trajectory as _traj_mod

if not hasattr(_traj_mod, "InMemoryTrajectoryRegistry"):
    _traj_mod.InMemoryTrajectoryRegistry = MagicMock

import jiuwenswarm.server.runtime.agent_adapter.interface_code as _ic_mod
from jiuwenswarm.common.config import resolve_env_vars


JiuwenSwarmCodeAdapter = _ic_mod.JiuwenSwarmCodeAdapter
_FIXED_RAIL_NAMES = JiuwenSwarmCodeAdapter._FIXED_RAIL_NAMES


def test_coding_artifact_post_process_rail_is_code_fixed_rail() -> None:
    assert "CodingArtifactPostProcessRail" in _FIXED_RAIL_NAMES
    assert "CodingArtifactPostProcessRail" not in _ic_mod._RAIL_BUILD_NAMES


def test_code_adapter_builds_coding_artifact_post_process_rail() -> None:
    adapter = JiuwenSwarmCodeAdapter()
    captured: list = []
    real_instantiate = adapter._instantiate_rails

    def _capture(rail_infos, _config_base):
        captured.extend(rail_infos)
        return rail_infos

    object.__setattr__(adapter, "_instantiate_rails", _capture)
    try:
        adapter._build_agent_rails(config={}, config_base={}, mode="code")
    finally:
        object.__setattr__(adapter, "_instantiate_rails", real_instantiate)

    matches = [
        info for info in captured
        if info.attr_name == "_coding_artifact_post_process_rail"
    ]
    assert len(matches) == 1
    assert all(info.attr_name != "_task_execution_rail" for info in captured)
    assert (
        matches[0].build_func
        == adapter._build_coding_artifact_post_process_rail
    )
    assert matches[0].params == {"coauthor_header_enabled": False}


def test_code_adapter_passes_enabled_coauthor_header_config() -> None:
    adapter = JiuwenSwarmCodeAdapter()
    captured: list = []
    real_instantiate = adapter._instantiate_rails

    def _capture(rail_infos, _config_base):
        captured.extend(rail_infos)
        return rail_infos

    object.__setattr__(adapter, "_instantiate_rails", _capture)
    try:
        adapter._build_agent_rails(
            config={},
            config_base={
                "modes": {
                    "code": {
                        "artifact_post_process": {"coauthor_header": True},
                    },
                },
            },
            mode="code",
        )
    finally:
        object.__setattr__(adapter, "_instantiate_rails", real_instantiate)

    match = next(
        info for info in captured
        if info.attr_name == "_coding_artifact_post_process_rail"
    )
    assert match.params == {"coauthor_header_enabled": True}


def test_parse_config_bool_handles_environment_strings() -> None:
    for value in ("1", "true", "TRUE", "yes", "on"):
        assert _ic_mod._parse_config_bool(value) is True

    for value in ("0", "false", "FALSE", "no", "off", ""):
        assert _ic_mod._parse_config_bool(value) is False

    assert _ic_mod._parse_config_bool("invalid") is False


def test_coauthor_header_env_placeholder_resolves_to_strict_bool(monkeypatch) -> None:
    key = "JIUWENSWARM_CODE_COAUTHOR_HEADER_ENABLED"

    monkeypatch.setenv(key, "true")
    assert (
        _ic_mod._parse_config_bool(
            resolve_env_vars(f"${{{key}:-false}}"),
        )
        is True
    )

    monkeypatch.setenv(key, "false")
    assert (
        _ic_mod._parse_config_bool(
            resolve_env_vars(f"${{{key}:-false}}"),
        )
        is False
    )

    monkeypatch.delenv(key)
    assert (
        _ic_mod._parse_config_bool(
            resolve_env_vars(f"${{{key}:-false}}"),
        )
        is False
    )


def test_code_adapter_treats_false_environment_string_as_disabled() -> None:
    adapter = JiuwenSwarmCodeAdapter()
    captured: list = []
    real_instantiate = adapter._instantiate_rails

    def _capture(rail_infos, _config_base):
        captured.extend(rail_infos)
        return rail_infos

    object.__setattr__(adapter, "_instantiate_rails", _capture)
    try:
        adapter._build_agent_rails(
            config={},
            config_base={
                "modes": {
                    "code": {
                        "artifact_post_process": {"coauthor_header": "false"},
                    },
                },
            },
            mode="code",
        )
    finally:
        object.__setattr__(adapter, "_instantiate_rails", real_instantiate)

    match = next(
        info
        for info in captured
        if info.attr_name == "_coding_artifact_post_process_rail"
    )
    assert match.params == {"coauthor_header_enabled": False}
