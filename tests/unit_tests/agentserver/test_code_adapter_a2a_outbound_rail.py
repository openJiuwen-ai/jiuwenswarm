# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Code-mode registration coverage for the A2A outbound toolkit rail."""

from __future__ import annotations

from unittest.mock import MagicMock

import openjiuwen.agent_evolving.trajectory as _trajectory_module
import pytest

if not hasattr(_trajectory_module, "InMemoryTrajectoryRegistry"):
    _trajectory_module.InMemoryTrajectoryRegistry = MagicMock

from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
    JiuwenSwarmCodeAdapter,
)


def _rail_attr_names(*, mode: str = "code", sub_mode: str | None = None) -> list[str]:
    adapter = JiuwenSwarmCodeAdapter()
    real_instantiate = adapter._instantiate_rails

    def _passthrough_instantiate(rail_infos, _config_base):
        return rail_infos

    object.__setattr__(adapter, "_instantiate_rails", _passthrough_instantiate)
    try:
        rail_infos = adapter._build_agent_rails(
            config={},
            config_base={},
            mode=mode,
            sub_mode=sub_mode,
        )
    finally:
        object.__setattr__(adapter, "_instantiate_rails", real_instantiate)
    return [rail_info.attr_name for rail_info in rail_infos]


@pytest.mark.parametrize("sub_mode", [None, "normal", "plan", "  "])
def test_code_single_agent_modes_include_a2a_outbound_rail(sub_mode):
    assert "_a2a_outbound_toolkit_rail" in _rail_attr_names(sub_mode=sub_mode)


def test_code_adapter_default_mode_reload_keeps_a2a_outbound_rail():
    assert "_a2a_outbound_toolkit_rail" in _rail_attr_names(
        mode="agent",
        sub_mode=None,
    )


@pytest.mark.parametrize("sub_mode", ["team", "TEAM", " team "])
def test_code_team_modes_exclude_adapter_owned_a2a_outbound_rail(sub_mode):
    assert "_a2a_outbound_toolkit_rail" not in _rail_attr_names(sub_mode=sub_mode)


def test_enterprise_code_mode_includes_a2a_outbound_rail(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    assert "_a2a_outbound_toolkit_rail" in _rail_attr_names(sub_mode="normal")
