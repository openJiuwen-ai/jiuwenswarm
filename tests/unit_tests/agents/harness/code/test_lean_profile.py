# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Lean coding profile (modes.code.profile: lean): config, filter rail, prompt, spec."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from openjiuwen.core.single_agent.prompts.builder import PromptSection
from openjiuwen.harness.prompts.builder import SystemPromptBuilder

from jiuwenswarm.agents.harness.code import lean_profile as lean_profile_module
from jiuwenswarm.agents.harness.code import spec as code_spec
from jiuwenswarm.agents.harness.code.lean_profile import (
    DEFAULT_DROP_PROMPT_SECTIONS,
    DEFAULT_SKIP_RAILS,
    DEFAULT_TOOLS_ALLOW,
    LeanToolFilterRail,
    filter_names,
    prune_block,
    resolve_code_profile,
)
from jiuwenswarm.agents.harness.code.prompt import code_prompt_builder
from jiuwenswarm.agents.harness.code.prompt.code_prompt_builder import (
    build_code_system_prompt,
)


@contextlib.contextmanager
def _capture_logger(caplog, target: logging.Logger, level: int = logging.WARNING):
    """Capture records from ``target`` on any pytest version.

    ``setup_logger()`` sets ``propagate = False`` on the ``jiuwenswarm`` logger,
    and agent-core's own loggers can be reconfigured while the suite runs, so
    records do not reliably reach the root handler that ``caplog`` installs on
    the pytest pinned by uv.lock (9.0). Attach caplog's handler to the emitting
    logger itself (as the base tests do) and stop propagation meanwhile, so
    each record is captured exactly once.
    """
    added = caplog.handler not in target.handlers
    saved = (target.propagate, target.level, caplog.handler.level)
    if added:
        target.addHandler(caplog.handler)
    target.propagate = False
    target.setLevel(level)
    caplog.handler.setLevel(level)
    try:
        yield
    finally:
        if added:
            target.removeHandler(caplog.handler)
        target.propagate = saved[0]
        target.setLevel(saved[1])
        caplog.handler.setLevel(saved[2])


def _lean_rail_logger() -> logging.Logger:
    """The stdlib logger behind agent-core's logger used by lean_profile.py."""
    return lean_profile_module.logger.logger()


def _cfg(**code):
    return {"modes": {"code": dict(code)}}


# ─── config ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "config",
    [None, {}, {"modes": {}}, _cfg(), _cfg(profile="default"), _cfg(profile=None)],
)
def test_profile_is_default_unless_lean_is_selected(config):
    assert resolve_code_profile(config).enabled is False


def test_unknown_profile_falls_back_to_default():
    assert resolve_code_profile(_cfg(profile="leen")).enabled is False


def test_lean_profile_uses_code_defaults():
    lean = resolve_code_profile(_cfg(profile="LEAN"))
    assert lean.enabled
    assert lean.tools_allow == DEFAULT_TOOLS_ALLOW
    assert lean.skip_rails == DEFAULT_SKIP_RAILS
    assert lean.drop_prompt_sections == DEFAULT_DROP_PROMPT_SECTIONS
    assert lean.rails_allow == ()
    assert lean.disable_subagents and lean.disable_progressive_tool


def test_lean_profile_overrides_are_parsed():
    lean = resolve_code_profile(
        _cfg(
            profile="lean",
            lean={
                "tools_allow": ["bash", " read_file ", ""],
                "rails_allow": "SkillUseRail",
                "skip_rails": [],
                "drop_prompt_sections": ["skills"],
                "disable_subagents": "false",
                "disable_progressive_tool": False,
            },
        )
    )
    assert lean.tools_allow == ("bash", "read_file")
    assert lean.rails_allow == ("SkillUseRail",)
    assert lean.skip_rails == ()
    assert lean.drop_prompt_sections == ("skills",)
    assert lean.disable_subagents is False
    assert lean.disable_progressive_tool is False


def test_malformed_lean_lists_keep_defaults():
    lean = resolve_code_profile(_cfg(profile="lean", lean={"tools_allow": 5}))
    assert lean.tools_allow == DEFAULT_TOOLS_ALLOW


def test_filter_names_preserves_order():
    assert filter_names(["b", "x", "a"], ["a", "b"]) == ["b", "a"]


# ─── filter rail ──────────────────────────────────────────────────────


def _builder(*names):
    builder = SystemPromptBuilder(language="en")
    for index, name in enumerate(names):
        builder.add_section(
            PromptSection(name=name, content={"en": f"# {name}\n"}, priority=10 + index)
        )
    return builder


def _ctx(tool_names, agent=None):
    tools = [SimpleNamespace(name=name) for name in tool_names]
    return SimpleNamespace(agent=agent, inputs=SimpleNamespace(tools=tools))


def test_filter_rail_keeps_only_allow_listed_tools_and_drops_sections():
    lean = resolve_code_profile(_cfg(profile="lean"))
    rail = LeanToolFilterRail(lean)
    builder = _builder("identity", "skills", "todo", "task_tool", "safety")
    rail.init(SimpleNamespace(system_prompt_builder=builder))
    ctx = _ctx(["bash", "task_tool", "read_file", "todo_create", "cron", "grep"])

    asyncio.run(rail.before_model_call(ctx))

    assert [tool.name for tool in ctx.inputs.tools] == ["bash", "read_file", "grep"]
    assert sorted(builder.get_all_sections()) == ["identity", "safety"]


def test_filter_rail_falls_back_to_ctx_agent_builder():
    rail = LeanToolFilterRail(resolve_code_profile(_cfg(profile="lean")))
    builder = _builder("identity", "skills")
    ctx = _ctx(["bash"], agent=SimpleNamespace(system_prompt_builder=builder))

    asyncio.run(rail.before_model_call(ctx))

    assert sorted(builder.get_all_sections()) == ["identity"]


def test_filter_rail_reads_names_from_cards_and_dicts():
    rail = LeanToolFilterRail(resolve_code_profile(_cfg(profile="lean")))
    tools = [
        SimpleNamespace(name="", card=SimpleNamespace(name="bash")),
        {"type": "function", "function": {"name": "grep"}},
        {"type": "function", "function": {"name": "task_tool"}},
    ]
    ctx = SimpleNamespace(agent=None, inputs=SimpleNamespace(tools=tools))

    asyncio.run(rail.before_model_call(ctx))

    assert len(ctx.inputs.tools) == 2


def test_filter_rail_never_raises():
    rail = LeanToolFilterRail(resolve_code_profile(_cfg(profile="lean")))
    broken = MagicMock()
    broken.has_section.side_effect = RuntimeError("boom")
    rail.system_prompt_builder = broken
    ctx = _ctx(["bash"])

    asyncio.run(rail.before_model_call(ctx))  # must not raise


def test_filter_rail_ignores_missing_tool_list():
    rail = LeanToolFilterRail(resolve_code_profile(_cfg(profile="lean")))
    ctx = SimpleNamespace(agent=None, inputs=SimpleNamespace(tools=None))
    asyncio.run(rail.before_model_call(ctx))
    assert ctx.inputs.tools is None


# ─── base system prompt ───────────────────────────────────────────────


def test_default_prompt_is_unchanged_by_the_profile_parameter():
    assert build_code_system_prompt() == build_code_system_prompt("default")
    default = build_code_system_prompt()
    for text in ("todo_create", "subagent_spawn", "ask_user", "list_files"):
        assert text in default


def test_lean_prompt_drops_features_the_profile_does_not_expose():
    lean = build_code_system_prompt("lean")
    for text in ("todo_create", "subagent", "task_tool", "ask_user", "list_files"):
        assert text not in lean
    for text in ("# Doing tasks", "## Bash usage rules", "## Parallel tool calls", "grep or glob"):
        assert text in lean
    assert len(lean) < len(build_code_system_prompt())


# ─── spec conversion ──────────────────────────────────────────────────


@pytest.fixture
def convert(monkeypatch):
    captured = {}

    class _Spec:  # records the kwargs the converter passes to DeepAgentSpec
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(code_spec, "DeepAgentSpec", _Spec)
    monkeypatch.setattr(code_spec, "_model_spec", lambda model: None)
    monkeypatch.setattr(code_spec, "_sys_operation_spec", lambda *_a: None)
    monkeypatch.setattr(code_spec, "WorkspaceSpec", lambda **kw: None)
    monkeypatch.setattr(code_spec, "is_subagent_runtime_enabled", lambda _cfg: True)
    monkeypatch.setattr(code_spec, "get_progressive_tool_enabled", lambda _cfg: True)

    def run(config_base):
        captured.clear()
        adapter = MagicMock()
        adapter._tool_owner_id.return_value = "owner"
        code_spec.convert_code_config_to_deep_agent_spec(
            adapter=adapter,
            config_base=config_base,
            react_config={},
            model=None,
            card=SimpleNamespace(id="main", name="main"),
            system_prompt="",
            workspace_root="/tmp",
            project_dir="/tmp",
            sys_operation=None,
            sys_operation_card=None,
            language="en",
            context_engine_config=None,
            kv_cache_affinity_config=None,
            enable_read_image_multimodal=None,
            completion_timeout=None,
        )
        return captured

    return run


_TOOLS = ["web_free_search", "web_fetch_webpage", "user_todos", "code_search", "skill_toolkit"]
_RAILS = ["SkillUseRail", "WorktreeRail"]


def test_default_profile_spec_is_unchanged(convert):
    spec = convert(_cfg(tools=list(_TOOLS), rails=list(_RAILS)))
    assert spec["tools"][0].params["configured_tools"] == _TOOLS
    assert spec["rails"][0].params["configured_rails"] == _RAILS
    assert len(spec["subagents"]) == 1
    assert spec["enable_subagent_runtime"] is True
    assert spec["progressive_tool"].enabled is True


def test_lean_profile_spec_drops_subagents_progressive_and_extra_tools(convert):
    spec = convert(_cfg(profile="lean", tools=list(_TOOLS), rails=list(_RAILS)))
    assert spec["tools"][0].params["configured_tools"] == ["code_search"]
    assert spec["rails"][0].params["configured_rails"] == []
    assert spec["subagents"] == []
    assert spec["enable_subagent_runtime"] is False
    assert spec["progressive_tool"].enabled is False


def test_lean_profile_can_keep_subagents_and_progressive_by_config(convert):
    spec = convert(
        _cfg(
            profile="lean",
            lean={"disable_subagents": False, "disable_progressive_tool": False},
        )
    )
    assert len(spec["subagents"]) == 1
    assert spec["enable_subagent_runtime"] is True
    assert spec["progressive_tool"].enabled is True


# ─── review fixes ─────────────────────────────────────────────────────


def test_run_tests_is_in_the_default_allow_list():
    assert "run_tests" in DEFAULT_TOOLS_ALLOW


def test_allow_list_matching_nothing_keeps_the_unfiltered_tools(caplog):
    lean = resolve_code_profile(_cfg(profile="lean", lean={"tools_allow": ["Bash", "readfile"]}))
    rail = LeanToolFilterRail(lean)
    rail.init(SimpleNamespace(system_prompt_builder=_builder("identity")))
    ctx = _ctx(["bash", "read_file", "edit_file"])

    with _capture_logger(caplog, _lean_rail_logger()):
        asyncio.run(rail.before_model_call(ctx))

    assert [tool.name for tool in ctx.inputs.tools] == ["bash", "read_file", "edit_file"]
    assert "matched none" in caplog.text


def test_unmatched_allow_list_names_are_warned_once(caplog):
    lean = resolve_code_profile(_cfg(profile="lean", lean={"tools_allow": ["bash", "raed_file"]}))
    rail = LeanToolFilterRail(lean)
    rail.init(SimpleNamespace(system_prompt_builder=_builder("identity")))

    with _capture_logger(caplog, _lean_rail_logger()):
        asyncio.run(rail.before_model_call(_ctx(["bash", "read_file"])))
        asyncio.run(rail.before_model_call(_ctx(["bash", "read_file"])))

    assert caplog.text.count("not registered in this agent") == 1
    assert "raed_file" in caplog.text


def test_prune_block_is_a_no_op_when_markers_are_absent():
    text = "# A\n\n### B\nkeep\n"
    assert prune_block(text, "### Missing\n", "### B") == text
    assert prune_block(text, "### B\n", "### Missing") == text
    assert prune_block("x\n## S\nend\n", "## S\n", None) == "x\n"


def test_filter_rail_prunes_feature_only_blocks_of_shared_sections():
    lean = resolve_code_profile(_cfg(profile="lean"))
    rail = LeanToolFilterRail(lean)
    builder = SystemPromptBuilder()
    boundaries = (
        "## JiuwenSwarm Internal Directories\n\n"
        "- `IDENTITY.md`, `memory/`, `todo/` are Agent internal data.\n\n"
        "### Installed Extension Assets\n\n- Installed Skill root: `/x`\n\n"
        "### Directory Usage Rules\n\n- Do not write ordinary task artifacts here.\n"
    )
    builder.add_section(PromptSection(name="directory_boundaries",
                                      content={"en": boundaries, "cn": boundaries}, priority=20))
    builder.add_section(PromptSection(name="input", priority=21, content={
        "en": "# Input Instructions\n\n## User Messages\n\nuser\n\n## System Messages\n\ncron\n"}))
    rail.init(SimpleNamespace(system_prompt_builder=builder))

    asyncio.run(rail.before_model_call(_ctx(["bash"])))

    kept = builder.get_section("directory_boundaries").content["en"]
    assert "Installed Extension Assets" not in kept and "Installed Skill root" not in kept
    assert "Do not write ordinary task artifacts" in kept and "`todo/`" in kept
    inp = builder.get_section("input").content["en"]
    assert "## User Messages" in inp and "System Messages" not in inp and "cron" not in inp


def test_lean_prompt_generators_survive_changed_headings(monkeypatch, caplog):
    monkeypatch.setattr(code_prompt_builder, "_TODO_SUBSECTION_START", "## Renamed heading\n")
    with _capture_logger(caplog, code_prompt_builder._lean_logger):
        section = code_prompt_builder._code_using_your_tools_lean_prompt()
    assert section.content["en"]
    assert "todo subsection markers not found" in caplog.text


def test_lean_session_guidance_keeps_the_no_answer_hunting_rule() -> None:
    """The lean profile drops tool guidance but keeps the rule against mining history/network.

    The default profile gains the same rule (same constant) from the repo-aware-tools MR.
    """
    from jiuwenswarm.agents.harness.code.prompt import code_prompt_builder as builder

    assert builder._NO_ANSWER_HUNTING_RULE.strip() in builder.build_code_system_prompt(profile="lean")
