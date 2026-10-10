# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""FlashSubagentRail 单元测试：spec 自注入 + flash 调优文案 + 机制复用。

锁定五点：
1. spec 自注入：开关开 → 注入 general-purpose spec 并注册 task_tool
   （flash 专属路径，绕过 interface_deep 的 mode 门控）；已有非 gp spec
   （research/browser/自定义 agent）时共存注入、原 spec 保留——修复
   「subagents 非空即静默不注入 gp」与 agent 模式同配置行为分叉的问题；
2. 开关关（react.subagents.general_agent.enabled 非 true）→ 跳过注册；
3. 工具卡描述与系统提示段为 flash 调优版（并行规则无条件、无强制委派条款），
   refresh_available_agents 刷新后不回退；
4. 已含 general-purpose spec（热重载路径）→ helper 按名去重，不重复注入；
5. CR-1 回归：flash 适配器 ``_build_subagent_rail`` 必须接受 rail 表注册时
   传入的 ``config_base`` 关键字参数（与 ``_build_task_planning_rail`` 同理），
   否则冷启动经 ``_instantiate_rails`` 构建时抛 TypeError。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.flash.rails.flash_subagent_rail import (
    FlashSubagentRail,
)
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.harness.prompts.sections import SectionName
from openjiuwen.harness.schema.config import SubAgentConfig


class _FakeBuilder:
    def __init__(self, language: str = "cn") -> None:
        self.language = language
        self.sections: list = []

    def add_section(self, section) -> None:
        self.sections = [s for s in self.sections if s.name != section.name]
        self.sections.append(section)

    def remove_section(self, name) -> None:
        self.sections = [s for s in self.sections if s.name != name]


class _FakeAbilityManager:
    def __init__(self) -> None:
        self.cards: dict = {}

    def add_ability(self, card, tool) -> None:
        self.cards[card.name] = card


class _FakeAgent:
    def __init__(self, subagents: list) -> None:
        self.deep_config = SimpleNamespace(subagents=list(subagents))
        self.ability_manager = _FakeAbilityManager()
        self.system_prompt_builder = _FakeBuilder()


def _make_rail(agent: _FakeAgent, inject: bool = True) -> FlashSubagentRail:
    """走真实构造（含父类全量属性），避免绕过 __init__ 后漏掉
    agent-core 新版 SubagentRail 增设的属性（enable_subagent_runtime 等）。"""
    rail = FlashSubagentRail(inject_general_purpose=inject)
    rail.init(agent)
    return rail


def _gp_spec() -> SubAgentConfig:
    """已存在的 general-purpose spec（真 SubAgentConfig，helper 去重可识别）。"""
    return SubAgentConfig(
        agent_card=AgentCard(name="general-purpose", description="通用测试子代理"),
        system_prompt="",
    )


def _research_spec() -> SubAgentConfig:
    """非 gp spec：模拟冷启动产物（research/browser/自定义 agent）。"""
    return SubAgentConfig(
        agent_card=AgentCard(name="research_agent", description="研究测试子代理"),
        system_prompt="",
    )


class TestSpecSelfInjection:
    def test_injects_when_empty_and_enabled(self) -> None:
        """空 subagents + 开关开 → 注入 spec 并注册 task_tool（flash 核心路径）。"""
        agent = _FakeAgent([])
        rail = _make_rail(agent, inject=True)

        assert rail.tools, "注入后必须注册工具"
        assert "task_tool" in agent.ability_manager.cards
        # deep_config.subagents 被注入 general-purpose spec
        specs = agent.deep_config.subagents
        assert len(specs) == 1
        assert isinstance(specs[0], SubAgentConfig)
        assert specs[0].agent_card.name == "general-purpose"

    def test_injects_alongside_existing_non_gp_specs(self) -> None:
        """P1 回归：已有非 gp spec（research/browser/自定义 agent）时 gp 仍注入。

        修复前 init 只在 subagents 为空时注入——冷启动带 research/browser/
        自定义 agent 的 flash 会话静默丢失 general-purpose（general_agent
        .enabled=true 被无视），且与 agent 模式同配置行为分叉（agent 模式
        gp 会追加进现有列表）。
        """
        existing = _research_spec()
        agent = _FakeAgent([existing])
        _make_rail(agent, inject=True)

        specs = agent.deep_config.subagents
        names = [spec.agent_card.name for spec in specs]
        # gp 注入头部（helper insert(0)），原 spec 保留
        assert names[0] == "general-purpose"
        assert names.count("general-purpose") == 1
        assert "research_agent" in names
        assert any(spec is existing for spec in specs)
        # task_tool 注册成功，可用类型同时含两种
        assert "task_tool" in agent.ability_manager.cards
        card = agent.ability_manager.cards["task_tool"]
        assert "general-purpose" in card.description
        assert "research_agent" in card.description

    def test_skips_when_disabled(self) -> None:
        """开关关（config 显式非 true）→ 不注入、不注册（对齐 stock 门控语义）。"""
        agent = _FakeAgent([])
        rail = _make_rail(agent, inject=False)

        assert not rail.tools
        assert not agent.deep_config.subagents
        assert "task_tool" not in agent.ability_manager.cards

    def test_no_double_injection_when_spec_exists(self) -> None:
        """已含 general-purpose spec（如热重载路径注入过）→ helper 按名去重，不重复注入。"""
        existing = _gp_spec()
        agent = _FakeAgent([existing])
        _make_rail(agent, inject=True)

        specs = agent.deep_config.subagents
        assert len(specs) == 1
        assert specs[0] is existing


class TestFlashDescriptions:
    def test_registers_and_rewrites_description(self) -> None:
        agent = _FakeAgent([_gp_spec()])
        _make_rail(agent)

        card = agent.ability_manager.cards.get("task_tool")
        assert card is not None, "task_tool 必须注册到 ability_manager"
        # flash 调优描述：并行规则无条件存在
        assert "必须在同一条消息中一次性发出全部 task_tool 调用" in card.description
        # available_agents 已格式化进描述
        assert "general-purpose" in card.description
        assert "{available_agents}" not in card.description
        # 不携带 stock 描述的标志性强制条款
        assert "永远不要委托理解" not in card.description

    def test_available_agents_lists_real_tool_face(self) -> None:
        """回归：available_agents 必须列出 rail 继承的文件/bash 工具。

        stock 构建只列 spec.tools（卡片），漏掉 rail 注册的文件工具，导致
        模型误判「子代理没有 read_file/bash」而放弃委派、全单干。
        CR-2 后文案与实际注册严格一致：不再宣称 todo/memory（对应 rail
        不在 spec 注入范围内），文件/bash 工具保留。
        """
        agent = _FakeAgent([_gp_spec()])
        _make_rail(agent)
        card = agent.ability_manager.cards["task_tool"]

        for tool_name in ("read_file", "write_file", "edit_file", "glob", "grep", "bash"):
            assert tool_name in card.description, (
                f"available_agents 必须包含 rail 继承工具 {tool_name}"
            )
        # 语义化能力声明（CLI/文件/调研）
        assert "执行 CLI 命令" in card.description
        # CR-2：不再虚报 todo / memory（对应 rail 不在 gp spec 注入范围）
        assert "todo" not in card.description
        assert "memory" not in card.description

    def test_subagent_spec_gets_flash_filesystem_rail(self) -> None:
        """CR-2 回归：gp spec 的 rails 字段必须包含 SlimSysOperationRail。

        冷启动时 cfg.rails 恒 None（rails 进 _pending_rails 不写
        deep_config），直接依赖会走 stock SysOperationRail fallback——
        gp 子代理获得 list_files/powershell 等主代理没有的工具。
        修复后 _inject_general_purpose_spec 主动构造 flash 变体传入。
        """
        from openjiuwen.harness.rails.sys_operation_rail import SysOperationRail
        from jiuwenswarm.agents.harness.flash import SlimSysOperationRail

        agent = _FakeAgent([])
        rail = _make_rail(agent, inject=True)

        specs = agent.deep_config.subagents
        assert len(specs) == 1
        gp_spec = specs[0]
        # spec 的 rails 字段包含 SlimSysOperationRail（flash 变体）
        spec_rails = getattr(gp_spec, "rails", None) or []
        assert any(isinstance(r, SlimSysOperationRail) for r in spec_rails), (
            "gp spec rails must include SlimSysOperationRail (flash variant), "
            f"got {[type(r).__name__ for r in spec_rails]}"
        )
        # 不含 stock SysOperationRail（flash 变体已满足 isinstance 检查，
        # 工厂不会再 fallback）
        assert not any(
            type(r) is SysOperationRail for r in spec_rails
        ), "gp spec rails should not contain stock SysOperationRail"

    def test_refresh_keeps_flash_description(self) -> None:
        agent = _FakeAgent([_gp_spec()])
        rail = _make_rail(agent)
        card = agent.ability_manager.cards["task_tool"]

        # DisabledToolsRail 刷新路径：不得回退为 stock 文案
        rail.refresh_available_agents(agent)
        assert "必须在同一条消息中一次性发出全部 task_tool 调用" in card.description
        assert "永远不要委托理解" not in card.description
        assert "read_file" in card.description


class TestFlashSection:
    @pytest.mark.asyncio
    async def test_before_model_call_injects_flash_section(self) -> None:
        agent = _FakeAgent([_gp_spec()])
        rail = _make_rail(agent)

        await rail.before_model_call(SimpleNamespace())
        builder: _FakeBuilder = agent.system_prompt_builder
        assert builder.sections, "必须注入 task_tool 提示段"
        section = builder.sections[-1]
        assert section.name == SectionName.TASK_TOOL
        content = section.content["cn"]
        # flash 段标志：按上下文压力判断 + 无条件并行 + 单干快路径 carve-out
        assert "按上下文压力判断" in content
        assert "在同一条消息中一次性发出全部 task_tool 调用" in content
        assert "直接执行更快" in content
        # 不携带 stock 段的强制委派条款（强模型拖慢源）
        assert "必须委派独立子代理处理，不得用 read_file 自行逐篇读取" not in content

    @pytest.mark.asyncio
    async def test_no_tools_no_section(self) -> None:
        agent = _FakeAgent([])
        rail = _make_rail(agent, inject=False)
        await rail.before_model_call(SimpleNamespace())
        assert not agent.system_prompt_builder.sections


class TestFlashCopyConstants:
    def test_bilingual_copies_present(self) -> None:
        from jiuwenswarm.agents.harness.flash.rails.flash_subagent_rail import (
            FLASH_TASK_SECTION,
            FLASH_TASK_TOOL_DESCRIPTION,
        )

        assert set(FLASH_TASK_SECTION) >= {"cn", "en"}
        assert set(FLASH_TASK_TOOL_DESCRIPTION) >= {"cn", "en"}
        for text in FLASH_TASK_SECTION.values():
            assert "task_tool" in text
        for text in FLASH_TASK_TOOL_DESCRIPTION.values():
            assert "{available_agents}" in text


class TestRailTableConstruction:
    def test_build_subagent_rail_accepts_config_base_kwarg(self) -> None:
        """CR-1 回归：flash ``_build_subagent_rail`` 必须接受 ``config_base``。

        冷启动 rail 表注册带 ``{"config_base": config_base}`` params，经
        ``_instantiate_rails`` 以 ``build_func(**info.params)`` 关键字调用；
        签名不匹配会抛 ``TypeError: unexpected keyword argument``，导致
        flash 会话冷启动 100% 失败（参考 review CR-1）。
        """
        from jiuwenswarm.server.runtime.agent_adapter.interface_flash import (
            JiuwenSwarmFlashAdapter,
        )

        adapter = JiuwenSwarmFlashAdapter()
        # 直接以关键字参数调用，模拟 _instantiate_rails 的调用方式
        rail = adapter._build_subagent_rail(config_base={"react": {}})
        assert isinstance(rail, FlashSubagentRail)

        # 无参调用也应兼容（旧路径 / 测试直接构建）
        rail_no_args = adapter._build_subagent_rail()
        assert isinstance(rail_no_args, FlashSubagentRail)
