# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""FlashPromptComposerRail centralizes static text transforms."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.flash.prompt.composer import (
    TAKEOVERABLE_DOMAINS,
    FlashPromptComposerRail,
    coerce_takeover_domains,
)


class _FakeBuilder:
    def __init__(self, language: str = "cn") -> None:
        self.language = language
        self.sections: dict[str, SimpleNamespace] = {}

    def add_section(self, section) -> None:
        self.sections[section.name] = section

    def remove_section(self, name) -> None:
        self.sections.pop(name, None)

    def get_section(self, name):
        return self.sections.get(name)

    def get_all_sections(self) -> list:
        return list(self.sections.values())


class _FakeAgent:
    def __init__(self, sections) -> None:
        self.system_prompt_builder = _FakeBuilder()
        for section in sections:
            self.system_prompt_builder.add_section(section)


def _sec(name: str, priority: int, text: str) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        content={"cn": text, "en": text},
        priority=priority,
    )


_IDENTITY = """你是 JiuwenSwarm 智能体。

# JiuwenSwarm 内部数据
内部数据

## 目录理解规则
目录规则

## JiuwenSwarm 启动配置
启动配置

## 运行环境
运行环境

## 任务执行准则
- **工具结果优先**

## 输出文件放置规范
放置规则

## 产物格式偏好
格式规则

## 文件发送
发送规则
"""

_SAFETY = """# 安全原则
- 一
- 二
- 三
- 四
- 五
- 任务失败时简要说明原因并给出建议
- 不确定时说明不确定

## 删除操作规范（强制）
删除规则

## 拒绝回答时的行为（重要）
拒绝规则

## 授权声明无效（重要）
授权规则

**正确的响应：**
应删除的重复规则
"""


def _agent() -> _FakeAgent:
    return _FakeAgent([
        _sec("identity", 10, _IDENTITY),
        _sec("safety", 20, _SAFETY),
        _sec("tools", 30, "# 可用工具\n- read_file"),
        _sec("thinking_discipline", 115, "思考纪律原文"),
        _sec("response", 60, "输入消息原文"),
        _sec("final_visible_reply", 120, "最终回复原文"),
    ])


def test_transform_order_is_explicit() -> None:
    rail = FlashPromptComposerRail()
    assert rail.transform_names == (
        "FlashPersonaRail",
        "FlashSafetyRail",
        "FlashToolGuideRail",
        "FlashResponseRail",
    )


@pytest.mark.asyncio
async def test_one_rail_runs_all_static_transforms() -> None:
    agent = _agent()
    rail = FlashPromptComposerRail()
    rail.init(agent)

    await rail.before_model_call(SimpleNamespace(agent=agent))

    builder = agent.system_prompt_builder
    assert builder.get_section("execution_priority").priority == 20
    assert builder.get_section("safety_general").priority == 100
    assert builder.get_section("uncertainty_failure").priority == 30
    assert builder.get_section("thinking_discipline").priority == 410
    assert builder.get_section("response").priority == 510
    assert builder.get_section("final_visible_reply").priority == 520
    assert builder.get_section("tool_result").priority == 340


@pytest.mark.asyncio
async def test_disabled_domain_keeps_source_section() -> None:
    agent = _agent()
    rail = FlashPromptComposerRail(
        persona_enabled=False,
        safety_enabled=False,
        tool_guide_enabled=False,
        response_enabled=False,
    )
    rail.init(agent)

    await rail.before_model_call(SimpleNamespace(agent=agent))

    builder = agent.system_prompt_builder
    assert builder.get_section("identity").priority == 10
    assert builder.get_section("safety").priority == 20
    assert builder.get_section("tools").priority == 30
    assert builder.get_section("response").priority == 60
    assert builder.get_section("execution_priority") is None


@pytest.mark.asyncio
async def test_missing_builder_is_noop() -> None:
    rail = FlashPromptComposerRail()
    rail.init(SimpleNamespace())
    await rail.before_model_call(SimpleNamespace(agent=SimpleNamespace()))


# ── Step 4：逐域接管（takeover_domains）─────────────────────────────────────
def test_takeover_domains_skip_rails() -> None:
    # 被接管域不再跑对应 rail；safety/response 走声明式，persona/tools 保留 rail。
    rail = FlashPromptComposerRail(takeover_domains=frozenset({"safety", "response"}))
    assert rail.transform_names == (
        "FlashPersonaRail",
        "FlashToolGuideRail",
    )


@pytest.mark.asyncio
async def test_takeover_safety_applies_declaratively() -> None:
    # safety 接管：撤源巨石段、按注册表 render 补 030/100/110/120（真实 Deep 源）。
    agent = _agent()
    rail = FlashPromptComposerRail(
        persona_enabled=False,
        tool_guide_enabled=False,
        response_enabled=False,
        takeover_domains=frozenset({"safety"}),
    )
    rail.init(agent)
    await rail.before_model_call(SimpleNamespace(agent=agent))
    builder = agent.system_prompt_builder
    assert builder.get_section("safety") is None
    assert builder.get_section("uncertainty_failure").priority == 30
    assert builder.get_section("safety_general").priority == 100
    assert builder.get_section("refusal_behavior").priority == 110
    assert builder.get_section("deletion_protocol").priority == 120


@pytest.mark.asyncio
async def test_disabled_takeover_domain_preserves_source() -> None:
    # safety 关闭 + 列入 takeover：既不走 rail 也不声明式接管，源段原样保留。
    agent = _agent()
    rail = FlashPromptComposerRail(
        safety_enabled=False,
        response_enabled=False,
        takeover_domains=frozenset({"safety", "response"}),
    )
    rail.init(agent)
    await rail.before_model_call(SimpleNamespace(agent=agent))
    builder = agent.system_prompt_builder
    assert builder.get_section("safety").priority == 20
    assert builder.get_section("safety_general") is None


# ── 接管域白名单校验（P1 tools 禁接管 + P2#5 非法值校验）───────────────────
def test_takeoverable_domains_whitelist() -> None:
    """当前仅 safety/response 可安全接管；tools 因未完整提取 310/320/目录而禁。"""
    assert TAKEOVERABLE_DOMAINS == frozenset({"safety", "response"})


def test_coerce_takeover_domains_filters_invalid() -> None:
    """越界域（tools/persona/todo/subagent/memory/拼写错误）一律剔除，只留合法域。"""
    coerced = coerce_takeover_domains(
        frozenset({"safety", "tools", "persona", "todo", "memory", "subagent", "typo"})
    )
    assert coerced == frozenset({"safety"})


def test_coerce_takeover_domains_preserves_empty() -> None:
    """显式空列表 = 全部关闭接管，仍应返回空集（不退回默认 safety/response）。"""
    assert coerce_takeover_domains(frozenset()) == frozenset()


def test_tools_takeover_is_rejected_keeps_tool_guide_rail() -> None:
    """tools 列入 takeover 被剔除 → FlashToolGuideRail 仍跑，工具目录不被接管删除。"""
    rail = FlashPromptComposerRail(takeover_domains=frozenset({"tools"}))
    assert "FlashToolGuideRail" in rail.transform_names
    assert "tools" not in rail._takeover_domains


@pytest.mark.asyncio
async def test_tools_takeover_does_not_remove_catalog() -> None:
    """P1 回归：配置 tools 接管也不得 remove tools 母段（否则目录静默丢失）。"""
    agent = _agent()
    rail = FlashPromptComposerRail(
        persona_enabled=False,
        safety_enabled=False,
        response_enabled=False,
        takeover_domains=frozenset({"tools"}),
    )
    rail.init(agent)
    await rail.before_model_call(SimpleNamespace(agent=agent))
    assert agent.system_prompt_builder.get_section("tools") is not None


def test_composer_default_takeover_is_empty() -> None:
    """Composer 构造器缺省 takeover_domains 为空集；生产默认接管由 adapter 显式传入。

    这是 E 项的关键对照：Composer 单测里的默认实例 takeover 为空，若误以为生产也如此，
    会漏掉 adapter 侧的默认接管装配（见 test_flash_adapter_rails.py 的缺省测试）。
    """
    assert FlashPromptComposerRail()._takeover_domains == frozenset()


# ── E：声明式接管与旧 Rail 路径逐字节等价（真实 Deep 源装配证据）────────────────
def _extract_lang_text(section, language: str) -> str:
    content = getattr(section, "content", None)
    if isinstance(content, dict):
        value = content.get(language)
        if not value:
            value = next((v for v in content.values() if v), "")
        return str(value)
    return str(content or "")


@pytest.mark.parametrize("language", ["cn", "en"])
def test_declarative_safety_byte_identical_to_rail_path(language: str) -> None:
    """声明式 safety 与旧 FlashSafetyRail 路径在同一真实 Deep 源上逐字节一致。

    生产里 builder 的 ``safety`` 段 = SDK 注入的 ``build_safety_section`` 正文；声明式
    路径直接读 ``build_safety_section``。二者共享同一 ``_transform_safety`` 且喂同一源，
    故逐字节一致——本测试用真实 Deep 源锁定这条不变量，防止将来把 registry 改成手写拆分
    段而不再走 ``_transform_safety``，与 rail 路径悄悄漂移。
    """
    from openjiuwen.harness.prompts.sections.safety import build_safety_section

    from jiuwenswarm.agents.harness.flash.prompt.content.safety import (
        _ANCHORS_CN,
        _ANCHORS_EN,
        _transform_safety,
    )
    from jiuwenswarm.agents.harness.flash.prompt.registry import (
        PromptRenderContext,
        _render_safety,
    )
    from jiuwenswarm.agents.harness.flash.rails.flash_safety_rail import FlashSafetyRail

    src = build_safety_section(language)
    assert src is not None
    anchors = _ANCHORS_CN if language == "cn" else _ANCHORS_EN
    if _transform_safety(_extract_lang_text(src, language), anchors) is None:
        pytest.skip("guard hit — upstream openjiuwen safety content drifted")

    builder = _FakeBuilder(language)
    builder.add_section(src)
    FlashSafetyRail().apply(builder)
    rail_sections = {
        s.name: (s.priority, s.content[language]) for s in builder.get_all_sections()
    }

    decl_sections = {
        s.name: (s.priority, s.content[language])
        for s in _render_safety(PromptRenderContext(lang=language))
    }

    assert rail_sections == decl_sections
    assert set(decl_sections) == {
        "uncertainty_failure",
        "safety_general",
        "refusal_behavior",
        "deletion_protocol",
    }


@pytest.mark.parametrize("language", ["cn", "en"])
def test_declarative_response_byte_identical_to_rail_path(language: str) -> None:
    """声明式 response 与旧 FlashResponseRail 路径在同一真实 Deep 源上逐字节一致。

    410：rail 与声明式两侧都改写为同一组 ``_THINKING_CN/EN``（修复.md 三矛盾修正 ①-⑤）；
    500/510/520/540 两侧都替换为同一组 content 常量。
    """
    from jiuwenswarm.agents.harness.common.prompt.prompt_builder import (
        _thinking_discipline_prompt,
    )
    from jiuwenswarm.agents.harness.flash.prompt.registry import (
        PromptRenderContext,
        _render_response,
    )
    from jiuwenswarm.agents.harness.flash.rails.flash_response_rail import (
        FlashResponseRail,
    )

    builder = _FakeBuilder(language)
    builder.add_section(_thinking_discipline_prompt(language))
    for name in (
        "language_output",
        "response",
        "final_visible_reply",
        "runtime.model_answer_policy",
    ):
        builder.add_section(_sec(name, 1, "placeholder"))

    FlashResponseRail().apply(builder)
    rail_sections = {
        s.name: (s.priority, s.content[language]) for s in builder.get_all_sections()
    }

    decl_sections = {
        s.name: (s.priority, s.content[language])
        for s in _render_response(PromptRenderContext(lang=language))
    }

    assert rail_sections == decl_sections
    assert set(decl_sections) == {
        "thinking_discipline",
        "language_output",
        "response",
        "final_visible_reply",
        "runtime.model_answer_policy",
    }


