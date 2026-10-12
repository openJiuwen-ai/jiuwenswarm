# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""JiuwenSwarmCodeAdapter rail assembly under the default and lean Code profiles."""

from __future__ import annotations

import importlib.util

import pytest

from jiuwenswarm.agents.harness.code.lean_profile import (
    DEFAULT_SKIP_RAILS,
    LeanToolFilterRail,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
    JiuwenSwarmCodeAdapter,
)

# Fixed rails of _build_agent_rails on this base, in order (permission rails
# are stubbed out below). The default profile must keep exactly this list.
_DEFAULT_FIXED_RAILS = [
    "_runtime_prompt_rail",
    "_response_prompt_rail",
    "_skill_retrieval_prompt_rail",
    "_symphony_orchestration_rail",
    "_stream_event_rail",
    "_security_rail",
    "_heartbeat_rail",
    "_session_messaging_route_rail",
    "_lsp_rail",
    "_project_memory_rail",
    "_code_filesystem_rail",
    "_coding_memory_rail",
    "_memory_forbidden_rail",
    "_code_agent_mode_rail",
    "_code_ask_user_rail",
    "_code_confirm_interrupt_rail",
    "_context_processor_rail",
    "_eternal_conversation_rail",
    "_code_execution_discipline_rail",
    "_implement_completeness_rail",
    "_code_completion_gate_rail",
    "_code_task_planning_rail",
    "_code_agent_rail",
    "_code_plan_approval_rail",
    "_design_rail",
    "_subagent_rail",
]

_CODE_EXECUTION_DISCIPLINE_AVAILABLE = (
    importlib.util.find_spec(
        "jiuwenswarm.agents.harness.code.rails.code_execution_discipline_rail"
    )
    is not None
)
if not _CODE_EXECUTION_DISCIPLINE_AVAILABLE:
    _DEFAULT_FIXED_RAILS = [
        name
        for name in _DEFAULT_FIXED_RAILS
        if name != "_code_execution_discipline_rail"
    ]



@pytest.fixture
def adapter(monkeypatch):
    instance = JiuwenSwarmCodeAdapter()
    captured: dict[str, list] = {}

    def fake_instantiate(rail_infos, _config_base):
        captured["infos"] = list(rail_infos)
        return [info.attr_name for info in rail_infos]

    monkeypatch.setattr(instance, "_instantiate_rails", fake_instantiate)
    monkeypatch.setattr(instance, "_permission_interrupt_rail_infos", lambda _cfg: [])
    instance._captured = captured  # type: ignore[attr-defined]
    return instance


def _config(**code):
    return {"modes": {"code": dict(code)}}


def test_default_profile_keeps_every_fixed_rail(adapter):
    names = adapter._build_agent_rails({}, _config(rails=["SkillUseRail"]))
    # Every base fixed rail is kept, in order; rails added by other Code-mode
    # features may be interleaved.
    kept = [n for n in names if n in _DEFAULT_FIXED_RAILS]
    assert kept == _DEFAULT_FIXED_RAILS
    assert "_lean_tool_filter_rail" not in names
    assert "_dynamic_SkillUseRail" in names


def test_lean_profile_skips_listed_rails_and_mounts_the_filter_last(adapter):
    names = adapter._build_agent_rails(
        {}, _config(profile="lean", rails=["SkillUseRail", "WorktreeRail"])
    )
    for skipped in DEFAULT_SKIP_RAILS:
        assert skipped not in names
        # adapter code reads these attributes directly; they must exist as None
        assert getattr(adapter, skipped) is None
    for kept in (
        "_code_filesystem_rail",
        "_project_memory_rail",
        *(
            ("_code_execution_discipline_rail",)
            if _CODE_EXECUTION_DISCIPLINE_AVAILABLE
            else ()
        ),
        "_implement_completeness_rail",
        "_code_completion_gate_rail",
        "_context_processor_rail",
    ):
        assert kept in names
    assert names[-1] == "_lean_tool_filter_rail"
    assert "_dynamic_SkillUseRail" not in names
    assert "_dynamic_WorktreeRail" not in names


def test_lean_filter_rail_builder_returns_the_rail(adapter):
    adapter._build_agent_rails({}, _config(profile="lean"))
    info = adapter._captured["infos"][-1]
    rail = info.build_func(**info.params)
    assert isinstance(rail, LeanToolFilterRail)
    assert rail.config.enabled


def test_lean_rails_allow_keeps_named_dynamic_rails(adapter):
    names = adapter._build_agent_rails(
        {},
        _config(profile="lean", rails=["SkillUseRail"], lean={"rails_allow": ["SkillUseRail"]}),
    )
    assert "_dynamic_SkillUseRail" in names


def test_lean_skip_rails_override(adapter):
    names = adapter._build_agent_rails(
        {}, _config(profile="lean", lean={"skip_rails": ["_lsp_rail"]})
    )
    assert "_lsp_rail" not in names
    assert "_subagent_rail" in names
