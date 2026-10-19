# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Flash Turbo discovery through real rail assembly and SDK model input."""

from __future__ import annotations

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

from jiuwenswarm.agents.harness.common.rails.progressive_tool_rail import ProgressiveToolRail
from jiuwenswarm.common import utils
from jiuwenswarm.common.local_env_config import bind_task_env_overlay, reset_task_env_overlay
from jiuwenswarm.server.runtime.agent_adapter import interface_deep, interface_flash
from jiuwenswarm.server.runtime.agent_adapter.interface_flash import JiuwenSwarmFlashAdapter
from jiuwenswarm.server.runtime.skill_turbo import skill_turbo_tools, turbo_package_loader

TURBO_TOOL_NAME = "skill_acceleration_exec"


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
    (codes / "ppt_gen_root.py").write_text("root = None\n", encoding="utf-8")
    (turbo / "meta.json").write_text(json.dumps({
        "external_name": "pptx-craft", "description": "生成 PPT", "match_keywords": ["ppt"],
    }), encoding="utf-8")
    monkeypatch.setattr(utils, "get_agent_skills_dir", lambda: empty)
    monkeypatch.setattr(skill_turbo_tools, "_entries_cache", {})
    monkeypatch.setattr(skill_turbo_tools, "_last_good_entries", {})
    monkeypatch.setattr(skill_turbo_tools.skill_turbo.card, "description", "")
    return empty, shared


def _assemble_flash(monkeypatch, *, enabled=True, disabled=None, lazy=True):
    adapter = object.__new__(JiuwenSwarmFlashAdapter)
    config = adapter._apply_flash_react_override({"react": {
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
    real_builders = {
        "_build_agent_rails", "_build_progressive_tool_rail",
        "_build_disabled_tools_rail",
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
    rails = adapter._build_agent_rails(config["react"], config, mode="flash")
    adapter._build_task_execution_rail.assert_not_called()
    adapter._build_skill_turbo_prompt_rail.assert_not_called()
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
async def test_relay_empty_prewarm_refreshes_before_first_real_model_call(
    monkeypatch, tmp_path, skill_roots, mode, lazy,
):
    _, shared = skill_roots
    with _skill_env():
        initial = skill_turbo_tools._build_tool_description()
        assert "当前内部支持：暂无" in initial
        skill_turbo_tools.skill_turbo.card.description = initial
        adapter, rails = _assemble_flash(monkeypatch, lazy=lazy)

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
        for _ in range(2):
            session = Session(session_id=f"relay-flash-{uuid4().hex}", card=core.card)
            with _skill_env(shared):
                await adapter._update_runtime_config(SimpleNamespace(mode="flash"))
                await session.pre_run(inputs={"query": "制作销售复盘 PPT"})
                if mode == "stream":
                    _ = [chunk async for chunk in core.stream(
                        {"query": "制作销售复盘 PPT"}, session=session,
                    )]
                else:
                    await core.invoke({"query": "制作销售复盘 PPT"}, session=session)
                await session.post_run()
        assert len(model.calls) == 2
        for messages, tools in model.calls:
            cards = [tool for tool in tools if tool.name == TURBO_TOOL_NAME]
            assert len(cards) == 1
            assert "当前内部支持：pptx-craft" in cards[0].description
            assert "暂无" not in cards[0].description
            assert not any("当前内部支持：暂无" in str(message.content) for message in messages)
            # Do not bring back the full Turbo guide (with legacy todo names).
            assert not any("## 技能加速通道" in str(message.content) for message in messages)
        # The second request reuses the existing roots-keyed TTL cache.
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
