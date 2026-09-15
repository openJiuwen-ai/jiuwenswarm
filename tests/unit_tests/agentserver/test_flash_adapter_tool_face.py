# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""flash 模式工具面单元测试：rail 白名单、react 覆盖、统一 todo / memory 工具。"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from openjiuwen.core.foundation.store.base_embedding import EmbeddingConfig

from jiuwenswarm.agents.harness.common.memory.config import (
    is_agent_mode,
    is_memory_enabled,
    is_proactive_memory,
)
from jiuwenswarm.agents.harness.flash import (
    FlashMemoryRail,
    FlashMemoryTool,
    UnifiedTodoTool,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
    _RailBuildInfo,
)
from jiuwenswarm.server.runtime.agent_adapter import interface_flash
from jiuwenswarm.server.runtime.agent_adapter.interface_flash import (
    JiuwenSwarmFlashAdapter,
)


def _bare_adapter(cls) -> object:
    """Bare adapter carrying no initialized state (class-level behavior only)."""
    return object.__new__(cls)


class _StubTodoEngine:
    """Records forwarded inputs/kwargs in place of a stock Todo engine."""

    def __init__(self) -> None:
        self.calls: list[tuple[dict, dict]] = []

    async def invoke(self, inputs: dict, **kwargs):
        self.calls.append((inputs, kwargs))
        return {"ok": True}


class _FakeAbilityManager:
    """Registry double keyed by ability name."""

    def __init__(self) -> None:
        self.tools: dict[str, object] = {}
        self.adds: list[str] = []

    def get(self, name: str):
        return self.tools.get(name)

    def add_ability(self, card, tool):
        self.tools[card.name] = card
        self.adds.append(card.name)
        return SimpleNamespace(added=True)


# ── rail 白名单 ──────────────────────────────────────────────────────


def test_keep_whitelist_retains_skill_and_security_rails() -> None:
    """技能链与 disabled_tools 的冷启动表项必须存活（reload 复活点对齐）。"""
    adapter = _bare_adapter(JiuwenSwarmFlashAdapter)
    keep = [
        "_skill_rail",
        "_skill_credential_injection_rail",
        "_skill_active_state_rail",
        "_skill_authorization_rail",
        "_disabled_tools_rail",
        "_permission_rail",
        "_filesystem_rail",
    ]
    drop = [
        "_skill_evolution_rail",
        "_skill_turbo_prompt_rail",
        "_a2a_outbound_toolkit_rail",
        "_symphony_orchestration_rail",
    ]
    infos = [_RailBuildInfo(name, lambda: None) for name in keep + drop]
    filtered = adapter._filter_rail_infos_by_keep(infos)
    assert [info.attr_name for info in filtered] == keep


# ── react 覆盖（eager_tools 整表替换 / evolution 深合并 / env 告警） ──


def test_react_override_replaces_eager_tools_and_keeps_siblings() -> None:
    """eager_tools 整表替换为 flash 名单，部署自有键与兄弟子树保留。"""
    adapter = _bare_adapter(JiuwenSwarmFlashAdapter)
    config_base = {
        "react": {
            "enable_task_loop": True,
            "tool_lazy_load": {
                "enabled": True,
                "eager_tools": ["web_search", "list_files", "todo_create"],
                "subagents": {"enabled": True},
            },
            "evolution": {"skill_create": True, "extra_key": "keep_me"},
        }
    }
    react = adapter._apply_flash_react_override(config_base)["react"]

    assert react["enable_task_loop"] is False
    lazy = react["tool_lazy_load"]
    assert lazy["eager_tools"] == [
        "web_search",
        "fetch_webpage",
        "ask_user",
        "read_file",
        "write_file",
        "edit_file",
        "glob",
        "grep",
        "bash",
        "skill_tool",
        "skill_complete",
        "todo",
    ]
    assert lazy["enabled"] is True
    assert lazy["subagents"] == {"enabled": True}
    assert react["evolution"]["skill_create"] is False
    assert react["evolution"]["extra_key"] == "keep_me"


def test_evolution_env_bypass_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    """env 级开关翻回 true 时必须告警（task_loop 有被 force-revive 的风险）。"""
    adapter = _bare_adapter(JiuwenSwarmFlashAdapter)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_flash.get_skill_create_enabled",
        lambda config: True,
    )

    # 仓库的 setup_logger()（common/utils.py 模块级调用）把 jiuwenswarm 命名空间
    # logger 置为 propagate=False，告警到不了 root；caplog 只挂 root，旧版
    # pytest 不补挂非传播 logger。直接在模块 logger 上捕获，不依赖传播行为。
    captured: list[str] = []

    class _CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record.getMessage())

    handler = _CaptureHandler()
    interface_flash.logger.addHandler(handler)
    try:
        adapter._apply_flash_react_override({"react": {}})
    finally:
        interface_flash.logger.removeHandler(handler)

    assert any("SKILL_CREATE" in message for message in captured)


# ── 技能模式 / 工具卡裁剪 ────────────────────────────────────────────


def test_skill_mode_pinned_all() -> None:
    """flash 钉死 ALL：auto_list 配置也不注册 ListSkillTool。"""
    assert (
        JiuwenSwarmFlashAdapter._resolve_skill_mode({"skill_mode": "auto_list"}) == "all"
    )


def test_tool_card_drop_names_flash_only() -> None:
    """flash 剔除 wiki/acp 工具卡；normal 适配器默认不剔除。"""
    flash = _bare_adapter(JiuwenSwarmFlashAdapter)
    deep = _bare_adapter(JiuWenSwarmDeepAdapter)
    assert flash._tool_card_drop_names() == frozenset(
        {"wiki_ingest", "wiki_query", "wiki_lint", "acp_chat"}
    )
    assert deep._tool_card_drop_names() == frozenset()


def test_slim_search_skill_card_id_distinct_from_stock() -> None:
    """slim search_skill 卡 id 必须与 stock 版区分。

    共享注册表按 card.id 先到先得，agent 模式 prewarm 先注册 stock 版
    "search_skill"；若 slim 版同 id，后注册为 no-op，flash 会话调到的
    将是不带 install 参数的 stock 实例。name 保持不变（模型可见面不变）。
    """
    from jiuwenswarm.agents.harness.flash.slim_skill_toolkit import (
        SlimSkillToolkit,
    )

    toolkit = SlimSkillToolkit(manager=None, service_id="svc", agent_id="ag")
    tools = toolkit.get_tools()

    assert [t.card.name for t in tools] == ["search_skill"]
    assert tools[0].card.id != "search_skill"


# ── 统一 todo 工具（get action） ─────────────────────────────────────


def test_unified_todo_card_exposes_get_action() -> None:
    tool = UnifiedTodoTool({}, language="cn")
    assert "get" in tool.card.input_params["properties"]["action"]["enum"]
    assert "id" in tool.card.input_params["properties"]


@pytest.mark.asyncio
async def test_unified_todo_get_forwards_id() -> None:
    engine = _StubTodoEngine()
    tool = UnifiedTodoTool({"get": engine}, language="cn")
    result = await tool.invoke({"action": "get", "id": "t1"}, session="s1")
    assert result == {"ok": True}
    assert engine.calls == [({"id": "t1"}, {"session": "s1"})]


@pytest.mark.asyncio
async def test_unified_todo_get_requires_id() -> None:
    tool = UnifiedTodoTool({"get": _StubTodoEngine()}, language="cn")
    with pytest.raises(ValueError):
        await tool.invoke({"action": "get"})


# ── flash memory rail（只读双源 / 恢复注册） ─────────────────────────


def _make_memory_rail() -> FlashMemoryRail:
    return FlashMemoryRail(
        embedding_config=EmbeddingConfig(
            model_name="m", base_url="http://embed", api_key="k"
        ),
        is_proactive=False,
    )


def test_flash_memory_rail_read_only_or() -> None:
    """读保护 = cron/heartbeat 只读 OR 群聊分身只读。"""
    rail = _make_memory_rail()
    assert rail._read_only_now() is False
    rail._is_read_only = True
    assert rail._read_only_now() is True
    rail._is_read_only = False
    rail.set_read_only(True)
    assert rail._read_only_now() is True
    rail.set_read_only(False)
    assert rail._read_only_now() is False


def test_flash_memory_rail_restore_memory_tool() -> None:
    """场景2 按名移除后恢复分支重新注册；未被移除时不重复注册。"""
    rail = _make_memory_rail()
    tool = FlashMemoryTool(None, language="cn")
    rail._unified_tool = tool
    agent = SimpleNamespace(ability_manager=_FakeAbilityManager())
    agent.ability_manager.tools["memory"] = tool.card

    rail.restore_memory_tool(agent)
    assert agent.ability_manager.adds == []

    del agent.ability_manager.tools["memory"]
    rail.restore_memory_tool(agent)
    assert agent.ability_manager.adds == ["memory"]


def test_flash_memory_rail_restore_without_tool_is_noop() -> None:
    rail = _make_memory_rail()
    agent = SimpleNamespace(ability_manager=_FakeAbilityManager())
    rail.restore_memory_tool(agent)
    assert agent.ability_manager.adds == []


# ── flash 记忆档归一 ─────────────────────────────────────────────────


def test_flash_mode_memory_reads_agent_profile() -> None:
    config = {"modes": {"agent": {"memory": {"enabled": True}}}}
    assert is_agent_mode("flash") is True
    assert is_memory_enabled("flash", config) is True
    assert is_proactive_memory("flash", config) is False
    # agent 档与未识别 mode 行为不变
    assert is_memory_enabled("agent", config) is True
    assert is_memory_enabled("team", config) is False
