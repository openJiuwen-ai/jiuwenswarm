# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Flash 静态提示词声明式注册表（Composer 的唯一组装入口）。

把「哪些 Deep 段要撤、哪些 Flash 段要补」从散落的各转换 Rail 收敛为**数据**。每个
Flash 自有静态域登记为一个 :class:`FlashPromptBlock`：

- ``owner`` / ``carrier`` / ``supersedes`` —— 声明替换关系（接管哪些 Deep 旧段、以
  何种载体补上）；
- ``enabled_when`` —— 声明门控（todo / subagent 依赖对应工具在位）；
- ``render`` —— 声明「由 :class:`PromptRenderContext` 生成该域全部段」的纯函数。

:func:`reconcile` 按注册表声明式产出 Flash 自有静态段（``PromptSection`` 列表，按段号
升序），是**运行时 Composer 与离线快照组装的同一实现**——保证运行时与 golden 快照零漂移。

与 ``flash.sections.FLASH_SECTION_REGISTRY``（priority → SectionSpec 领号表，供 snapshot
做归属/载体校验）互补：本注册表回答「怎么生成/替换」，领号表回答「段号归属谁、载体是什么」。

设计说明（对应 ``FlashAgent设计/1.md`` 隐患 2 修正版）：注册表以**域**为单位登记——一个
block 可产多段（safety 一拆四），段名/段号由 ``render`` 产出的
``PromptSection`` 携带，不在此重复声明；``render`` 产 ``tuple[PromptSection, ...]``
而非单个 ``str``，避免「一段一 render 却要共享同一 Deep 源」的割裂。

``supersedes`` 语义为「本域接管/替换的 Deep 旧段名」——不严格等于「全撤」：safety
是撤巨石换多段；response / todo / subagent 是同名迁号改写（``add_section`` 按名
覆盖，无需显式 remove）；tools 是部分剥离（去重规则 / 工具结果末条移入 3xx，源段本体保留）。
具体撤/写动作在 Step 4 各域接管时落地，本表只声明责任归属。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Mapping

from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.prompts.sections import SectionName
from openjiuwen.harness.prompts.sections.safety import build_safety_section
from openjiuwen.harness.prompts.sections.todo import NO_MODEL_SELECTION_PROMPT

from jiuwenswarm.agents.harness.flash.prompt.content.response import (
    _FINAL_CN,
    _FINAL_EN,
    _LANGUAGE_CN,
    _LANGUAGE_EN,
    _MODEL_CN,
    _MODEL_EN,
    _NAME_FINAL,
    _NAME_LANGUAGE,
    _NAME_MODEL,
    _NAME_RESPONSE,
    _NAME_THINKING,
    _PRIO_FINAL,
    _PRIO_LANGUAGE,
    _PRIO_MODEL,
    _PRIO_RESPONSE,
    _PRIO_THINKING,
    _RESPONSE_CN,
    _RESPONSE_EN,
    _THINKING_CN,
    _THINKING_EN,
)
from jiuwenswarm.agents.harness.flash.prompt.content.safety import (
    _ANCHORS_CN,
    _ANCHORS_EN,
    _transform_safety,
)
from jiuwenswarm.agents.harness.flash.prompt.content.subagent import FLASH_TASK_SECTION
from jiuwenswarm.agents.harness.flash.prompt.content.todo import (
    _TODO_DISCIPLINE_CN,
    _TODO_DISCIPLINE_EN,
    _TODO_SECTION_PRIORITY,
)
from jiuwenswarm.agents.harness.flash.prompt.content.tools import (
    _BASH_CN,
    _BASH_EN,
    _NAME_BASH,
    _NAME_RESULT,
    _NAME_SELECTION,
    _PRIO_BASH,
    _PRIO_RESULT,
    _PRIO_SELECTION,
    _RESULT_CN,
    _RESULT_EN,
    _SELECTION_CN,
    _SELECTION_EN,
)
from jiuwenswarm.agents.harness.flash.sections import get_section_order

__all__ = [
    "FLASH_PROMPT_REGISTRY",
    "FlashPromptBlock",
    "PromptRenderContext",
    "build_default_context",
    "reconcile",
]

# 440 子代理委派段目标段号（content.subagent 未导出优先级常量，rail 内联 440）。
_PRIO_SUBAGENT = 440


@dataclass(frozen=True)
class PromptRenderContext:
    """轻量只读渲染上下文（对应 1.md 隐患 2 修正版）。

    ``lang`` 恒为规范化后的 ``cn`` / ``en``；``enabled_tools`` 是当前工具面的冻结集合，
    供 ``enabled_when`` 门控；``skill_catalog`` / ``persona_override`` 为 7xx/010 动态域
    预留（Step 4 后填充），``extra`` 承载平台注入（供 persona 域未来纳入快照）。
    """

    lang: Literal["cn", "en"] = "cn"
    enabled_tools: frozenset[str] = frozenset()
    skill_catalog: str | None = None
    persona_override: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_cn(self) -> bool:
        return self.lang == "cn"


@dataclass(frozen=True)
class FlashPromptBlock:
    """一个 Flash 自有静态域的声明式登记（一域可产多段）。"""

    key: str
    owner: Literal["deep", "flash"] = "flash"
    carrier: Literal["system", "durable", "attachment"] = "system"
    supersedes: tuple[str, ...] = ()
    enabled_when: Callable[[PromptRenderContext], bool] | None = None
    render: Callable[[PromptRenderContext], tuple[PromptSection, ...]] | None = None


def _resolve_language(language: str) -> str:
    """与 flash rail 的 _resolve_language 语义一致（cn/zh → cn，否则 en）。"""
    return "cn" if language in ("cn", "zh") else "en"


def _section_text(section, language: str) -> str:
    """从 PromptSection 取指定语言正文；无该键时回退第一个非空值。"""
    content = getattr(section, "content", None)
    if isinstance(content, dict):
        value = content.get(language)
        if not value:
            value = next((v for v in content.values() if v), "")
        return str(value)
    return str(content or "")


def _tool_enabled(tool: str) -> Callable[[PromptRenderContext], bool]:
    return lambda ctx: tool in ctx.enabled_tools


# ── 各域 render（与 flash 转换 rail / offline 组装同构，输出逐字节一致）──────────────
def _render_safety(ctx: PromptRenderContext) -> tuple[PromptSection, ...]:
    src = build_safety_section(ctx.lang)
    if src is None:
        raise AssertionError(
            "build_safety_section returned None — upstream safety source missing"
        )
    content = _section_text(src, ctx.lang)
    anchors = _ANCHORS_CN if ctx.is_cn else _ANCHORS_EN
    result = _transform_safety(content, anchors)
    if result is None:
        raise AssertionError(
            "safety transform guard hit on real Deep source — upstream safety content "
            "drifted; flash split/rewrite anchors no longer match"
        )
    return tuple(
        PromptSection(name=name, content={ctx.lang: text}, priority=priority)
        for name, priority, text in result
    )


def _render_response(ctx: PromptRenderContext) -> tuple[PromptSection, ...]:
    # 410：三矛盾修正正文（修复.md ①-⑤），迁号 + 改写为 06 目标正文。
    cn = ctx.is_cn
    thinking = _THINKING_CN if cn else _THINKING_EN
    targets = (
        (_NAME_THINKING, _PRIO_THINKING, thinking),
        (_NAME_LANGUAGE, _PRIO_LANGUAGE, _LANGUAGE_CN if cn else _LANGUAGE_EN),
        (_NAME_RESPONSE, _PRIO_RESPONSE, _RESPONSE_CN if cn else _RESPONSE_EN),
        (_NAME_FINAL, _PRIO_FINAL, _FINAL_CN if cn else _FINAL_EN),
        (_NAME_MODEL, _PRIO_MODEL, _MODEL_CN if cn else _MODEL_EN),
    )
    return tuple(
        PromptSection(name=name, content={ctx.lang: text}, priority=priority)
        for name, priority, text in targets
    )


def _render_tools(ctx: PromptRenderContext) -> tuple[PromptSection, ...]:
    """300/330/340 三段的 06 目标正文（content.tools 硬编码，无动态源）。"""
    cn = ctx.is_cn
    targets = (
        (_NAME_SELECTION, _PRIO_SELECTION, _SELECTION_CN if cn else _SELECTION_EN),
        (_NAME_BASH, _PRIO_BASH, _BASH_CN if cn else _BASH_EN),
        (_NAME_RESULT, _PRIO_RESULT, _RESULT_CN if cn else _RESULT_EN),
    )
    return tuple(
        PromptSection(name=name, content={ctx.lang: text}, priority=priority)
        for name, priority, text in targets
    )


def _render_todo(ctx: PromptRenderContext) -> tuple[PromptSection, ...]:
    """430 todo 纪律段：三段合一正文 + SDK 模型选择说明（与 FlashTodoRail 同构）。"""
    discipline = _TODO_DISCIPLINE_CN if ctx.is_cn else _TODO_DISCIPLINE_EN
    model_note = NO_MODEL_SELECTION_PROMPT.get(ctx.lang, NO_MODEL_SELECTION_PROMPT["cn"])
    text = discipline + model_note
    return (
        PromptSection(
            name=SectionName.TODO,
            content={ctx.lang: text},
            priority=_TODO_SECTION_PRIORITY,
        ),
    )


def _render_subagent(ctx: PromptRenderContext) -> tuple[PromptSection, ...]:
    """440 子代理委派段（sync 分支正文，与 FlashSubagentRail 同构）。"""
    text = FLASH_TASK_SECTION.get(ctx.lang, FLASH_TASK_SECTION["cn"])
    return (
        PromptSection(
            name=SectionName.TASK_TOOL,
            content={ctx.lang: text},
            priority=_PRIO_SUBAGENT,
        ),
    )


# ── 注册表（顺序即域接管顺序；段内/跨域最终按段号排序）────────────────────────────
FLASH_PROMPT_REGISTRY: tuple[FlashPromptBlock, ...] = (
    FlashPromptBlock(
        key="safety",
        supersedes=("safety",),
        render=_render_safety,
    ),
    FlashPromptBlock(
        key="response",
        supersedes=(
            "thinking_discipline",
            "language_output",
            "response",
            "final_visible_reply",
            "runtime.model_answer_policy",
        ),
        render=_render_response,
    ),
    FlashPromptBlock(
        key="tools",
        supersedes=("tools", "execution_principles"),
        render=_render_tools,
    ),
    FlashPromptBlock(
        key="todo",
        supersedes=("todo",),
        enabled_when=_tool_enabled("todo"),
        render=_render_todo,
    ),
    FlashPromptBlock(
        key="subagent",
        supersedes=("task_tool",),
        enabled_when=_tool_enabled("task_tool"),
        render=_render_subagent,
    ),
)


def build_default_context(language: str = "cn") -> PromptRenderContext:
    """构建离线/默认渲染上下文：两工具域（todo/subagent）全开，语言规范化。"""
    return PromptRenderContext(
        lang=_resolve_language(language),
        enabled_tools=frozenset({"todo", "task_tool"}),
    )


def reconcile(ctx: PromptRenderContext) -> list[PromptSection]:
    """按注册表声明式生成 Flash 自有静态段，返回按段号（priority）升序的列表。

    门控域（``enabled_when`` 为 False）跳过；``render`` 为 None 的域跳过（占位登记）。

    每个产出段在返回前都要经过领号表校验：段号必须已在
    ``sections.FLASH_SECTION_REGISTRY`` 登记（``get_section_order`` 未登记即抛
    ``KeyError``），杜绝「注册表产出野号段」——段号单一真相源 = 领号表。
    """
    sections: list[PromptSection] = []
    for block in FLASH_PROMPT_REGISTRY:
        if block.enabled_when is not None and not block.enabled_when(ctx):
            continue
        if block.render is not None:
            sections.extend(block.render(ctx))
    sections.sort(key=lambda section: section.priority)
    for section in sections:
        get_section_order(int(section.priority))
    return sections
