# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Flash Turbo discovery through real rail assembly and SDK model input."""

from __future__ import annotations

import builtins
import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from openjiuwen.core.foundation.llm import AssistantMessage, ModelClientConfig, ModelRequestConfig
from openjiuwen.core.foundation.llm.schema.message_chunk import AssistantMessageChunk
from openjiuwen.core.runner import Runner
from openjiuwen.core.session.agent import Session
from openjiuwen.harness import create_deep_agent
from openjiuwen.harness.prompts import PromptSection, SystemPromptBuilder
from openjiuwen.harness.rails.skills.skill_use_rail import SkillUseRail

from jiuwenswarm.agents.harness.common.rails.progressive_tool_rail import ProgressiveToolRail
from jiuwenswarm.agents.harness.flash.skill_selection import execution, service
from jiuwenswarm.common import utils
from jiuwenswarm.common.local_env_config import bind_task_env_overlay, reset_task_env_overlay
from jiuwenswarm.server.runtime.agent_adapter import interface_deep, interface_flash
from jiuwenswarm.server.runtime.agent_adapter.interface_flash import JiuwenSwarmFlashAdapter
from jiuwenswarm.server.runtime.skill_turbo import skill_turbo_tools, turbo_package_loader

TURBO_TOOL_NAME = "skill_acceleration_exec"
PPT_QUERIES = (
    "生成环保碳中和汇报 PPT，全程不出现任何人像 使用 pptx-craft 技能",
    "幻灯片 生成环保碳中和汇报 PPT，全程不出现任何人像 使用 pptx-craft 技能",
    "生成环保碳中和汇报 PPT，全程不出现任何人像",
)


@contextmanager
def _skill_env(root=None):
    # Relay supplies these roots in the per-request env, not the process spawn env.
    token = bind_task_env_overlay(
        {} if root is None else {"JIUWENSWARM_SHARED_SKILLS_DIRS": str(root)}
    )
    try:
        yield
    finally:
        reset_task_env_overlay(token)


@pytest.fixture
def skill_roots(tmp_path, monkeypatch):
    empty = tmp_path / "relay-default-skills"
    empty.mkdir()
    shared = tmp_path / "office-claw-skills"
    turbo = shared / "pptx-craft" / "turbo"
    codes = turbo / "turbo_codes" / "ppt"
    codes.mkdir(parents=True)
    (shared / "pptx-craft" / "SKILL.md").write_text(
        "---\nname: pptx-craft\ndescription: Create PPT presentations\n---\n# Workflow\nGenerate a deck.\n",
        encoding="utf-8",
    )
    (codes / "ppt_gen_root.py").write_text("root = None\n", encoding="utf-8")
    (turbo / "meta.json").write_text(json.dumps({
        "external_name": "pptx-craft", "description": "生成 PPT", "match_keywords": ["ppt"],
    }), encoding="utf-8")
    monkeypatch.setattr(utils, "get_agent_skills_dir", lambda: empty)
    monkeypatch.setattr(skill_turbo_tools, "_entries_cache", {})
    monkeypatch.setattr(skill_turbo_tools, "_last_good_entries", {})
    monkeypatch.setattr(skill_turbo_tools.skill_turbo.card, "description", "")
    monkeypatch.setattr(execution, "_runtime", None)
    monkeypatch.setattr(execution, "_startup_gate", execution.StartupGate())
    monkeypatch.setattr(service, "_services", service.OrderedDict())
    yield empty, shared
    if execution._runtime is not None:
        execution._runtime.shutdown()


def _assemble_flash(monkeypatch, *, enabled=True, disabled=None, lazy=True, selection=False, skill_root=None):
    adapter = object.__new__(JiuwenSwarmFlashAdapter)
    config = adapter._apply_flash_react_override({"flash": {"skill_selection": {"enabled": selection}}, "react": {
        "skill_turbo": {"enabled": enabled},
        "tool_lazy_load": {"enabled": lazy},
        "disabled_tools": disabled or [],
    }})
    monkeypatch.setattr(interface_deep, "get_config", lambda: config)
    monkeypatch.setattr(interface_flash, "get_config", lambda: config)
    adapter._model = adapter._instance = None
    adapter._config_cache = config["react"]
    adapter._config_base_cache = config
    adapter._instance_overrides = {}
    adapter._stream_event_rail = adapter._checkpointer = None
    adapter._skill_turbo_prompt_rail = None
    adapter._skill_rail = None
    real_builders = {
        "_build_agent_rails", "_build_progressive_tool_rail",
        "_build_disabled_tools_rail", "_build_skill_turbo_prompt_rail",
        "_build_flash_skill_selection_rail",
    }
    for name in dir(JiuwenSwarmFlashAdapter):
        if name.startswith("_build_") and name not in real_builders:
            monkeypatch.setattr(adapter, name, Mock(return_value=None))
    monkeypatch.setattr(interface_deep, "_build_context_processor_rail", lambda **_: None)
    monkeypatch.setattr(adapter, "_load_extra_rails_from_env", lambda: [])
    monkeypatch.setattr(adapter, "_bind_checkpointer_to_rails", lambda: None)
    monkeypatch.setattr(adapter, "_filesystem_rail_enabled_for_profile", lambda: False)
    monkeypatch.setattr(adapter, "_task_execution_rail_enabled", lambda: True)
    monkeypatch.setattr(adapter, "_resolve_runtime_language", lambda: "cn")
    monkeypatch.setattr(adapter, "_tool_owner_id", lambda: "flash-discovery-" + uuid4().hex)
    monkeypatch.setattr(adapter, "_get_progressive_active_skill", lambda: None)
    # The parent prepares unrelated memory, model, and channel tools. Keep the
    # real Flash request hook and exercise its ordering before the real SDK.
    monkeypatch.setattr(interface_deep.JiuWenSwarmDeepAdapter, "_update_runtime_config", AsyncMock())
    monkeypatch.setattr(adapter, "_sync_unified_memory_tool_visibility", Mock())
    if skill_root is not None:
        native = SkillUseRail([str(skill_root)], skill_mode="all", include_tools=False)
        monkeypatch.setattr(adapter, "_build_skill_rail", lambda **_: native)
    rails = adapter._build_agent_rails(config["react"], config, mode="flash")
    adapter._build_task_execution_rail.assert_not_called()
    adapter._build_skill_protocol_prompt_rail.assert_not_called()
    return adapter, rails


class _Model:
    def __init__(self):
        self.model_client_config = ModelClientConfig(
            client_provider="OpenAI", api_key="fake", api_base="https://fake.invalid",
        )
        self.model_config = ModelRequestConfig(model_name="fake")
        self.calls = []

    def _record(self, messages, kwargs):
        self.calls.append((list(messages), kwargs.get("tools") or []))

    async def invoke(self, messages, **kwargs):
        self._record(messages, kwargs)
        return AssistantMessage(content="完成测试")

    async def stream(self, messages, **kwargs):
        self._record(messages, kwargs)
        yield AssistantMessageChunk(content="完成测试")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["invoke", "stream"])
@pytest.mark.parametrize("lazy", [True, False])
@pytest.mark.parametrize("selection", [False, True])
async def test_relay_empty_prewarm_refreshes_before_first_real_model_call(
    monkeypatch, tmp_path, skill_roots, mode, lazy, selection,
):
    _, shared = skill_roots
    with _skill_env():
        initial = skill_turbo_tools._build_tool_description()
        assert "当前内部支持：暂无" in initial
        skill_turbo_tools.skill_turbo.card.description = initial
        adapter, rails = _assemble_flash(monkeypatch, lazy=lazy, selection=selection, skill_root=shared)

    scans = Mock(wraps=turbo_package_loader.discover_external_turbo_skills)
    monkeypatch.setattr(turbo_package_loader, "discover_external_turbo_skills", scans)
    monkeypatch.setattr("openjiuwen.harness.deep_agent.schedule_image_support_probe", lambda *_: None)
    model = _Model()
    await Runner.start()
    core = create_deep_agent(
        model=model, tools=[], rails=rails, system_prompt="Test",
        enable_task_loop=False, max_iterations=2, workspace=str(tmp_path / "workspace"),
    )
    adapter._instance = core
    try:
        await core._ensure_initialized()
        # Real adapter registration follows the Turbo switch and shared tool registry.
        adapter._init_skill_turbo_tool()
        assert core.ability_manager.get(TURBO_TOOL_NAME) is not None
        # Reuse the assembled SDK for all entry texts; config combinations remain independent.
        for query in PPT_QUERIES:
            session = Session(session_id=f"relay-flash-{uuid4().hex}", card=core.card)
            with _skill_env(shared):
                await adapter._update_runtime_config(SimpleNamespace(mode="flash"))
                await session.pre_run(inputs={"query": query})
                if mode == "stream":
                    _ = [chunk async for chunk in core.stream(
                        {"query": query}, session=session,
                    )]
                else:
                    await core.invoke({"query": query}, session=session)
                await session.post_run()
        assert len(model.calls) == len(PPT_QUERIES)
        for query, (messages, tools) in zip(PPT_QUERIES, model.calls, strict=True):
            cards = [tool for tool in tools if tool.name == TURBO_TOOL_NAME]
            assert len(cards) == 1
            assert "当前内部支持：pptx-craft" in cards[0].description
            assert "暂无" not in cards[0].description
            assert not any("当前内部支持：暂无" in str(message.content) for message in messages)
            # The model needs routing guidance, not just a visible tool card.
            system_text = "\n".join(str(message.content) for message in messages if message.role == "system")
            assert "## 技能加速通道" in system_text
            assert "todo_modify" not in system_text
            assert "当前支持：pptx-craft" in system_text
            assert "第一个工具调用必须是 skill_acceleration_exec" in system_text
            assert "这条规则不依赖技能检索开关" in system_text
            assert "__skill_turbo_hitl__" in system_text
            assert any(message.content == query for message in messages if message.role == "user")
            tool_names = [tool.name for tool in tools]
            assert ("search_installed_skills" in tool_names) is selection
        selection_rail = adapter._flash_skill_selection_rail
        assert selection_rail._last_error is None
        assert (selection_rail.service is not None) is selection
        # Subsequent entry texts reuse the existing roots-keyed TTL cache.
        assert scans.call_count == 1
    finally:
        await core.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,disabled", [
    (False, []), (True, [TURBO_TOOL_NAME]), (True, TURBO_TOOL_NAME),
])
async def test_disabled_turbo_is_not_refreshed_or_exposed(
    monkeypatch, tmp_path, skill_roots, enabled, disabled,
):
    adapter, rails = _assemble_flash(monkeypatch, enabled=enabled, disabled=disabled)
    if enabled:
        assert adapter._skill_turbo_prompt_rail in rails
        assert adapter._skill_turbo_prompt_rail._acceleration_disabled is True
    else:
        assert adapter._skill_turbo_prompt_rail is None
    progressive = next(rail for rail in rails if isinstance(rail, ProgressiveToolRail))
    assert TURBO_TOOL_NAME not in progressive.eager_tools
    refresh = Mock(wraps=skill_turbo_tools.refresh_skill_acceleration_description)
    monkeypatch.setattr(skill_turbo_tools, "refresh_skill_acceleration_description", refresh)
    monkeypatch.setattr("openjiuwen.harness.deep_agent.schedule_image_support_probe", lambda *_: None)
    model = _Model()
    await Runner.start()
    core = create_deep_agent(
        model=model, tools=[], rails=rails, system_prompt="Test",
        enable_task_loop=False, max_iterations=2, workspace=str(tmp_path / "workspace"),
    )
    adapter._instance = core
    try:
        await core._ensure_initialized()
        adapter._init_skill_turbo_tool()
        if not enabled:
            assert core.ability_manager.get(TURBO_TOOL_NAME) is None
        else:
            # A disabled rail must clear a guide left in the prompt builder.
            core.system_prompt_builder.add_section(PromptSection(
                name="skill_turbo_guide", content={"cn": "stale turbo guide"}, priority=8,
            ))
        with _skill_env(skill_roots[1]):
            await adapter._update_runtime_config(SimpleNamespace(mode="flash"))
            session = Session(session_id=f"flash-off-{uuid4().hex}", card=core.card)
            await session.pre_run(inputs={"query": "制作 PPT"})
            await core.invoke({"query": "制作 PPT"}, session=session)
            await session.post_run()
        refresh.assert_not_called()
        assert model.calls
        for messages, tools in model.calls:
            assert TURBO_TOOL_NAME not in [tool.name for tool in tools]
            assert not any(TURBO_TOOL_NAME in str(message.content) for message in messages)
            assert not any("stale turbo guide" in str(message.content) for message in messages)
    finally:
        await core.stop()


@pytest.mark.asyncio
async def test_refresh_updates_a_copied_card_and_skips_missing_or_disabled_tool(
    monkeypatch, skill_roots,
):
    adapter, _ = _assemble_flash(monkeypatch)
    card = skill_turbo_tools.skill_turbo.card.model_copy(deep=True)
    card.description = "stale"
    adapter._instance = SimpleNamespace(ability_manager=SimpleNamespace(get=lambda _: card))
    with _skill_env(skill_roots[1]):
        await adapter._update_runtime_config(SimpleNamespace(mode="flash"))
    assert "当前内部支持：pptx-craft" in card.description
    refresh = Mock(side_effect=AssertionError("must not scan"))
    monkeypatch.setattr(skill_turbo_tools, "refresh_skill_acceleration_description", refresh)
    adapter._config_cache["skill_turbo"]["enabled"] = False
    await adapter._update_runtime_config(SimpleNamespace(mode="flash"))
    adapter._config_cache["skill_turbo"]["enabled"] = True
    adapter._instance.ability_manager.get = lambda _: None
    await adapter._update_runtime_config(SimpleNamespace(mode="flash"))
    refresh.assert_not_called()


@pytest.mark.asyncio
async def test_turbo_guide_follows_request_roots_and_actual_tool_visibility(monkeypatch, tmp_path, skill_roots):
    empty, shared = skill_roots
    adapter, rails = _assemble_flash(monkeypatch)
    monkeypatch.setattr("openjiuwen.harness.deep_agent.schedule_image_support_probe", lambda *_: None)
    model = _Model()
    await Runner.start()
    core = create_deep_agent(
        model=model, tools=[], rails=rails, system_prompt="Test",
        enable_task_loop=False, max_iterations=2, workspace=str(tmp_path / "workspace"),
    )
    adapter._instance = core
    try:
        await core._ensure_initialized()
        adapter._init_skill_turbo_tool()
        for root, present, expected in [(empty, True, False), (shared, True, True),
                                        (empty, True, False), (shared, True, True),
                                        (shared, False, False)]:
            if not present:
                core.ability_manager.remove(TURBO_TOOL_NAME)
            with _skill_env(root):
                await adapter._update_runtime_config(SimpleNamespace(mode="flash"))
                session = Session(session_id=f"flash-roots-{uuid4().hex}", card=core.card)
                await session.pre_run(inputs={"query": "使用 pptx-craft 生成 PPT"})
                await core.invoke({"query": "使用 pptx-craft 生成 PPT"}, session=session)
                await session.post_run()
            messages, _ = model.calls[-1]
            text = "\n".join(str(m.content) for m in messages if m.role == "system")
            assert ("## 技能加速通道" in text) is expected
    finally:
        await core.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ["import", "constructor"])
async def test_turbo_prompt_build_failure_keeps_flash_usable(
    monkeypatch, tmp_path, skill_roots, failure_kind,
):
    module_name = "jiuwenswarm.agents.harness.flash.rails.flash_turbo_prompt_rail"
    error = ImportError("Turbo prompt dependency unavailable") if failure_kind == "import" else RuntimeError(
        "Turbo prompt constructor failed",
    )
    warning = Mock(wraps=interface_flash.logger.warning)
    with monkeypatch.context() as faults:
        faults.setattr(interface_flash.logger, "warning", warning)
        if failure_kind == "import":
            original_import = builtins.__import__

            def failing_import(name, *args, **kwargs):
                if name == module_name:
                    raise error
                return original_import(name, *args, **kwargs)

            faults.setattr(builtins, "__import__", failing_import)
        else:
            from jiuwenswarm.agents.harness.flash.rails import flash_turbo_prompt_rail

            faults.setattr(flash_turbo_prompt_rail, "FlashTurboPromptRail", Mock(
                side_effect=error,
            ))
        adapter, rails = _assemble_flash(monkeypatch, skill_root=skill_roots[1])
    assert adapter._skill_turbo_prompt_rail is None
    warning.assert_any_call("[FlashAdapter] FlashTurboPromptRail create failed: %s", error, exc_info=True)
    assert any(isinstance(rail, ProgressiveToolRail) for rail in rails)
    monkeypatch.setattr("openjiuwen.harness.deep_agent.schedule_image_support_probe", lambda *_: None)
    model = _Model()
    await Runner.start()
    core = create_deep_agent(
        model=model, tools=[], rails=rails, system_prompt="Test",
        enable_task_loop=False, max_iterations=2, workspace=str(tmp_path / "workspace"),
    )
    adapter._instance = core
    try:
        await core._ensure_initialized()
        adapter._init_skill_turbo_tool()
        with _skill_env(skill_roots[1]):
            await adapter._update_runtime_config(SimpleNamespace(mode="flash"))
            session = Session(session_id=f"flash-guide-failure-{uuid4().hex}", card=core.card)
            await session.pre_run(inputs={"query": "你好"})
            await core.invoke({"query": "你好"}, session=session)
            await session.post_run()
        assert len(model.calls) == 1
        messages, tools = model.calls[0]
        assert any(tool.name == TURBO_TOOL_NAME for tool in tools)
        assert not any("## 技能加速通道" in str(message.content) for message in messages)
    finally:
        await core.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("language,title", [("cn", "## 技能加速通道"), ("en", "## Skill Acceleration Channel")])
async def test_flash_guide_preserves_priority_and_cleans_up(monkeypatch, skill_roots, language, title):
    adapter, _ = _assemble_flash(monkeypatch)
    rail = adapter._skill_turbo_prompt_rail
    builder = SystemPromptBuilder(language=language)
    builder.add_section(PromptSection(
        name="skill_turbo_guide", content={language: "stale"}, priority=321,
    ))
    agent = SimpleNamespace(system_prompt_builder=builder)
    rail.init(agent)
    with _skill_env(skill_roots[1]):
        await rail.before_model_call(SimpleNamespace(
            agent=agent, inputs=SimpleNamespace(tools=[skill_turbo_tools.skill_turbo.card]),
        ))
    section = builder.get_section("skill_turbo_guide")
    assert section.priority == 321
    assert title in section.content[language]
    rail.uninit(agent)
    assert builder.get_section("skill_turbo_guide") is None
    assert rail.system_prompt_builder is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["import", "refresh", "guide", "priority", "add_section"])
async def test_flash_guide_runtime_failure_cleans_up_and_recovers(monkeypatch, failure_stage):
    from jiuwenswarm.agents.harness.flash.rails import flash_turbo_prompt_rail

    rail = flash_turbo_prompt_rail.FlashTurboPromptRail()
    builder = SystemPromptBuilder(language="cn")
    builder.add_section(PromptSection(
        name="skill_turbo_guide", content={"cn": "stale"}, priority=321,
    ))
    builder.add_section(PromptSection(name="unrelated", content={"cn": "keep"}, priority=322))
    agent = SimpleNamespace(system_prompt_builder=builder)
    rail.init(agent)
    ctx = SimpleNamespace(agent=agent, inputs=SimpleNamespace(tools=[skill_turbo_tools.skill_turbo.card]))
    monkeypatch.setattr(skill_turbo_tools, "refresh_skill_acceleration_description", Mock(return_value=["pptx-craft"]))
    error = ImportError("Turbo refresh import failed") if failure_stage == "import" else RuntimeError(
        "Turbo guide runtime failure",
    )
    warning = Mock()
    monkeypatch.setattr(flash_turbo_prompt_rail.logger, "warning", warning)
    with monkeypatch.context() as faults:
        if failure_stage == "import":
            original_import = builtins.__import__

            def failing_import(name, *args, **kwargs):
                if name == "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools":
                    raise error
                return original_import(name, *args, **kwargs)

            faults.setattr(builtins, "__import__", failing_import)
        elif failure_stage == "refresh":
            faults.setattr(skill_turbo_tools, "refresh_skill_acceleration_description", Mock(side_effect=error))
        elif failure_stage == "guide":
            faults.setattr(flash_turbo_prompt_rail, "region_edit_keyword_summary", Mock(side_effect=error))
        elif failure_stage == "priority":
            faults.setattr(rail, "_resolve_priority", Mock(side_effect=error))
        else:
            faults.setattr(builder, "add_section", Mock(side_effect=error))
        # Call the rail directly so the SDK cannot mask a missing local exception handler.
        await rail.before_model_call(ctx)
    assert builder.get_section("skill_turbo_guide") is None
    assert builder.get_section("unrelated").content == {"cn": "keep"}
    message = "refresh description" if failure_stage in ("import", "refresh") else "build skill_turbo_guide section"
    warning.assert_called_once_with(f"[FlashTurboPromptRail] {message} failed: %s", error)
    await rail.before_model_call(ctx)
    assert "pptx-craft" in builder.get_section("skill_turbo_guide").content["cn"]
    assert warning.call_count == 1
