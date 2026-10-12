# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for context-diet wiring in JiuwenSwarmCodeAdapter.

Covers:
- the trim rail is a fixed rail, built by default and skipped when disabled;
- the compaction processors are appended to ContextProcessorRail per config;
- disabled compaction leaves the base rail untouched;
- a failing add-on never removes the base ContextProcessorRail.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import openjiuwen.agent_evolving.trajectory as _traj_mod

if not hasattr(_traj_mod, "InMemoryTrajectoryRegistry"):
    _traj_mod.InMemoryTrajectoryRegistry = MagicMock

import jiuwenswarm.server.runtime.agent_adapter.interface_code as _ic_mod
from jiuwenswarm.agents.harness.code.rails import ContextDietTrimRail

JiuwenSwarmCodeAdapter = _ic_mod.JiuwenSwarmCodeAdapter


def _adapter(monkeypatch, code_cfg: dict) -> JiuwenSwarmCodeAdapter:
    monkeypatch.setattr(_ic_mod, "get_config", lambda: {"modes": {"code": code_cfg}})
    adapter = JiuwenSwarmCodeAdapter()
    adapter._code_spec_config_snapshot = None
    return adapter


def test_trim_rail_is_fixed_and_built_by_default(monkeypatch):
    assert "ContextDietTrimRail" in JiuwenSwarmCodeAdapter._FIXED_RAIL_NAMES
    adapter = _adapter(monkeypatch, {})
    rail = adapter._build_context_diet_trim_rail()
    assert isinstance(rail, ContextDietTrimRail)
    assert rail._config.bash_max_lines == 45 and rail._config.read_file_max_lines == 0


def test_trim_rail_honours_config_values_and_switch(monkeypatch):
    adapter = _adapter(monkeypatch, {"context_diet": {"trim": {"bash_max_lines": 300, "read_file_max_lines": 600}}})
    rail = adapter._build_context_diet_trim_rail()
    assert rail._config.bash_max_lines == 300 and rail._config.read_file_max_lines == 600

    adapter = _adapter(monkeypatch, {"context_diet": {"trim": {"enabled": False}}})
    assert adapter._build_context_diet_trim_rail() is None


def test_trim_rail_in_fixed_rail_build_list(monkeypatch):
    adapter = _adapter(monkeypatch, {})
    captured = {}

    def fake_instantiate(rail_infos, config_base):
        captured["attrs"] = [info.attr_name for info in rail_infos]
        return []

    monkeypatch.setattr(adapter, "_instantiate_rails", fake_instantiate)
    adapter._build_agent_rails({}, {"modes": {"code": {}}})
    assert "_context_diet_trim_rail" in captured["attrs"]
    # Queued after the completion gate and before task planning. Only the
    # relative order is asserted: other changes may add rails in between.
    attrs = captured["attrs"]
    assert attrs.count("_context_diet_trim_rail") == 1
    trim = attrs.index("_context_diet_trim_rail")
    assert attrs.index("_code_completion_gate_rail") < trim
    assert trim < attrs.index("_code_task_planning_rail")


def test_compaction_processors_added_to_context_rail(monkeypatch):
    adapter = _adapter(monkeypatch, {"context_diet": {"compaction": {"shell_keep_last_k": 5, "read_keep_last_k": 9}}})
    base_rail = SimpleNamespace(add_processors=MagicMock())
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep._build_context_processor_rail",
        lambda cfg: base_rail,
    )
    rail = adapter._build_context_processor_rail()
    assert rail is base_rail
    pairs = base_rail.add_processors.call_args.args[0]
    names = [name for name, _ in pairs]
    assert names == ["ContextDietShellWindowProcessor", "ContextDietReadWindowProcessor"]
    assert pairs[0][1].keep_last_k == 5 and pairs[1][1].keep_last_k == 9
    assert "edit_file" not in pairs[0][1].tool_names and "write_file" not in pairs[0][1].tool_names


def test_compaction_disabled_leaves_base_rail_untouched(monkeypatch):
    adapter = _adapter(monkeypatch, {"context_diet": {"compaction": {"enabled": False}}})
    base_rail = SimpleNamespace(add_processors=MagicMock())
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep._build_context_processor_rail",
        lambda cfg: base_rail,
    )
    assert adapter._build_context_processor_rail() is base_rail
    base_rail.add_processors.assert_not_called()


def test_compaction_failure_keeps_base_rail(monkeypatch):
    adapter = _adapter(monkeypatch, {})
    base_rail = SimpleNamespace(add_processors=MagicMock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep._build_context_processor_rail",
        lambda cfg: base_rail,
    )
    assert adapter._build_context_processor_rail() is base_rail


def test_missing_base_rail_returns_none(monkeypatch):
    adapter = _adapter(monkeypatch, {})
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep._build_context_processor_rail",
        lambda cfg: None,
    )
    assert adapter._build_context_processor_rail() is None


def test_compaction_processors_use_the_real_context_processor_rail_contract(monkeypatch):
    """add_processors must run before init(); the pairs land in the rail's user processors."""
    import pytest
    from openjiuwen.harness.rails.context_engineer.context_processor_rail import (
        ContextProcessorRail,
    )

    adapter = _adapter(monkeypatch, {"context_diet": {"compaction": {"shell_keep_last_k": 3}}})
    real_rail = ContextProcessorRail(preset=True)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep._build_context_processor_rail",
        lambda cfg: real_rail,
    )
    rail = adapter._build_context_processor_rail()
    assert rail is real_rail
    keys = [key for key, _ in real_rail._user_processors]
    assert "ContextDietShellWindowProcessor" in keys
    cfg = dict(real_rail._user_processors)["ContextDietShellWindowProcessor"]
    assert cfg.keep_last_k == 3
    # the SDK contract: no additions after init()
    real_rail._initialized = True
    with pytest.raises(RuntimeError):
        real_rail.add_processors([("ContextDietReadWindowProcessor", cfg)])
