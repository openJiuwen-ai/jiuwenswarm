# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""FlashSubagentRail — task_tool 的 flash 调优变体（spec 自注入 + 文案替换）。

与 stock :class:`SubagentRail` 的差异（动机：不能降低现有性能，且改动仅
作用于 flash mode，不触碰 interface_deep / normal 路径）：
- **spec 自注入**：flash 冷启动门控（``mode.startswith("agent")``）不含
  flash，工厂不注入 general-purpose 子代理；本 rail 在 init 时用 agent-core
  的 ``_inject_general_purpose_subagent`` 从父配置自注入——传入现有 specs
  （research/browser/自定义 agent 等冷启动产物），助手按名去重后把
  general-purpose 插到列表头部，与 agent 模式同配置行为一致；派发侧
  ``_find_subagent_spec`` 动态读取，注入即生效。开关由 flash 适配器按
  ``react.subagents.general_agent.enabled`` 传入（显式 true 才注入，
  对齐 stock ``_is_subagent_enabled`` 语义）。
- **系统提示段替换**：stock 段落的「读 2 篇及以上文档必须委派、不得
  read_file 自读」会把强模型的单干快路径（一次性读完技能规范后直接执行）
  拖成逐篇委派的冷启动串行；flash 版改为**按上下文压力判断**——中间结果
  会显著膨胀主上下文的活（逐页研究 / 大规模抓取 / 多轮修复）才委派，读少
  量规范文档、单步操作明确「直接执行更快」。
- **并行机制无条件写入**：「多个互不依赖的子任务必须在同一条消息中一次性
  发出全部 task_tool 调用」在段落与工具卡描述中均为主规则（stock 版该机制
  写在工具卡描述的「用户明确要求并行」条款下，小模型易跳过导致串行派发）。
- **工具卡描述精简**：保留 subagent_type / task_description / model_tier
  语义与 spawn 要点，去掉与 flash 单轮语义无关的长篇指引。

复用 stock 的全部机制（继承）：注册 / uninit / available_agents 构建 /
DisabledToolsRail 刷新协议 / model resolver 与授权绑定（作用于 task_tool
工具本身，与本 rail 类型无关）。子代理继承侧由 agent-core factory 按
``isinstance(r, SubagentRail)`` 排除本 rail（防递归），子类同样命中。
"""

from __future__ import annotations

from typing import Dict

from openjiuwen.core.common.logging import logger
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts.builder import PromptSection
from openjiuwen.harness.prompts.sections import SectionName
from openjiuwen.harness.rails.subagent.subagent_rail import SubagentRail

# ---------------------------------------------------------------------------
# flash 调优文案（系统提示段 / 工具卡描述，中英双语）
# ---------------------------------------------------------------------------

FLASH_TASK_SECTION_CN = """\
## task_tool — 子代理委派：隔离上下文执行重活

子代理在独立上下文中运行：其全部中间工具调用结果不进入你的主上下文，仅最终摘要返回。\
这是长任务保持主上下文清晰、避免能力随上下文膨胀而下降的关键手段。

何时委派（按上下文压力判断，非强制）：
- 任务的中间结果会显著膨胀主上下文：逐页深度研究（产出多篇研究文件）、\
大规模抓取/解析/转换、多轮迭代的修复循环
- 主上下文已经较长时，新的大块独立工作优先委派
- 多个互不依赖的子任务必须并行执行：在同一条消息中一次性发出全部 task_tool 调用；\
每条消息只发一个调用等于串行等待，会成倍拉长总耗时

何时不委派（直接执行更快）：
- 一次性读取少量规范/配置/技能文档（SKILL.md 与若干 references 属于此列）
- 单步工具调用即可完成的简单操作
- 需要完整对话历史作为背景、或需要实时向用户流式输出的任务

使用要点：
- task_description 必须完整自包含——子代理没有本次对话的任何记忆
- 子代理结果对用户不可见；需要向用户展示时，另行发送文字总结
"""

FLASH_TASK_SECTION_EN = """\
## task_tool — delegate heavy work to isolated subagent contexts

Subagents run in an isolated context: none of their intermediate tool results \
enter your context; only the final summary returns. This is the key lever for \
keeping the main context clean on long tasks.

When to delegate (judge by context pressure, not by mandate):
- The subtask's intermediate results would significantly bloat your context: \
per-page deep research, large-scale fetch/parse/transform, multi-round fix loops
- When your context is already long, prefer delegating new large independent work
- Multiple independent subtasks MUST run in parallel: issue ALL task_tool calls \
together in a single message; one call per message serializes execution and \
multiplies wall-clock time

When NOT to delegate (doing it yourself is faster):
- Reading a handful of spec/config/skill documents (SKILL.md plus a few references)
- Simple operations completable in a single tool call
- Tasks needing full conversation history, or live streaming to the user

Notes:
- task_description must be fully self-contained — the subagent has no memory \
of this conversation
- Subagent results are not visible to the user; send a text summary when needed
"""

FLASH_TASK_SECTION: Dict[str, str] = {
    "cn": FLASH_TASK_SECTION_CN,
    "en": FLASH_TASK_SECTION_EN,
}

FLASH_TASK_TOOL_DESCRIPTION_CN = """\
启动子代理在隔离上下文中执行复杂任务，仅返回最终摘要（中间工具结果不进入主上下文）。

可用代理类型：
{available_agents}

调用参数：
- subagent_type：必填，必须与上方列表中的名称完全一致
- task_description：完整自包含的任务描述——子代理没有本次对话的任何记忆，\
需写明目标、背景、期望产出与验收标准
- model_tier / model_name：可选，为该子代理指定 lite/pro 档位或精确模型；\
省略时使用当前默认模型

并行规则：多个互不依赖的子任务，必须在同一条消息中一次性发出全部 task_tool 调用；\
每条消息只发一个调用会导致串行等待。
"""

FLASH_TASK_TOOL_DESCRIPTION_EN = """\
Launch a subagent to execute a complex task in an isolated context; only the \
final summary returns (intermediate tool results stay out of your context).

Available subagent types:
{available_agents}

Parameters:
- subagent_type: required; must exactly match a name from the list above
- task_description: a fully self-contained task brief — the subagent has no \
memory of this conversation; state the goal, background, expected output, \
and acceptance criteria
- model_tier / model_name: optional; assign a lite/pro tier or an exact model \
to this subagent; omit to use the current default model

Parallel rule: for multiple independent subtasks, issue ALL task_tool calls \
together in a single message; issuing one call per message serializes execution.
"""

FLASH_TASK_TOOL_DESCRIPTION: Dict[str, str] = {
    "cn": FLASH_TASK_TOOL_DESCRIPTION_CN,
    "en": FLASH_TASK_TOOL_DESCRIPTION_EN,
}

# flash 版 available_agents：general-purpose 的工具面语义化描述。
#
# stock `_build_available_agents_description` 只列 spec.tools（_get_tool_cards
# 注册的卡片：web_flash、deepresearch 系列等），漏掉 rail 继承在子代理构建时
# 注册的文件/todo/memory 工具——描述会写成「继承文件读写、bash」却在 Tools
# 括号里只列 deepresearch/web_flash，自相矛盾，模型据此误判「子代理干不了
# 文件活」而放弃委派、退回全单干，废掉上下文隔离。flash 工具面为类常量固定，
# 语义化描述不漂移；非 general-purpose 的 spec 回退 stock 构建。
FLASH_AVAILABLE_AGENTS_CN = (
    "- general-purpose: 通用型子代理，在隔离上下文中执行任务，仅最终摘要返回主上下文。"
    "继承主代理的完整工具面（Tools: read_file、write_file、edit_file、glob、grep、"
    "bash、todo、memory、web_flash、deepresearch 系列等）——"
    "可读写文件、执行 CLI 命令、联网搜索与深度调研，适合逐页研究、页面生成/修复等重活。"
)

FLASH_AVAILABLE_AGENTS_EN = (
    "- general-purpose: general-purpose subagent executing tasks in an isolated context; "
    "only the final summary returns to the main context. Inherits the parent's full tool "
    "face (Tools: read_file, write_file, edit_file, glob, grep, bash, todo, memory, "
    "web_flash, deepresearch series, etc.) — it can read/write files, run CLI commands, "
    "and do web/deep research; suited for heavy work like per-page research and page "
    "generation/fixing."
)

FLASH_AVAILABLE_AGENTS: Dict[str, str] = {
    "cn": FLASH_AVAILABLE_AGENTS_CN,
    "en": FLASH_AVAILABLE_AGENTS_EN,
}

_SUBAGENT_TOOL_NAMES = {"task_tool", "sessions_spawn"}


class FlashSubagentRail(SubagentRail):
    """task_tool 的 flash 调优 rail：spec 自注入 + flash 文案。

    flash 冷启动门控（interface_deep ``create_instance`` 的
    ``mode.startswith("agent")`` 检查）不含 flash，工厂不会注入
    general-purpose 子代理；本 rail 在 init 时（开关允许则）用 agent-core
    的 ``_inject_general_purpose_subagent`` 从父配置自注入 spec——传入
    现有 specs 由助手按名去重：已含 gp（热重载路径注入过）原样保留，
    未含则插入头部、与 research/browser/自定义 agent 共存（对齐 agent
    模式同配置行为，修复「subagents 非空即静默丢 gp」）。
    派发侧 ``_find_subagent_spec`` 是动态读取，注入即生效。
    所有改动仅作用于 flash mode（本 rail 只被 flash 适配器构建）。
    """

    def __init__(self, inject_general_purpose: bool = True) -> None:
        super().__init__()
        self.inject_general_purpose = bool(inject_general_purpose)

    def _resolve_language(self) -> str:
        language = getattr(self.system_prompt_builder, "language", None)
        return language if isinstance(language, str) and language else "cn"

    def _inject_general_purpose_spec(self, cfg) -> bool:
        """向 deep_config.subagents 注入 general-purpose spec（flash 专属路径）。

        复用 agent-core 的注入助手：rails 继承父代理的非 SubagentRail rails
        （本 rail 因 isinstance 检查被排除，防递归）、tools/model/workspace/
        sys_operation 均取父配置。传入现有 specs（research/browser/自定义
        agent 等冷启动产物）而非空列表，由助手按名去重——已含
        general-purpose（如热重载路径注入过）原样返回，未含则插入头部、
        现有 specs 全部保留。
        """
        from openjiuwen.harness.factory import _inject_general_purpose_subagent

        existing = list(getattr(cfg, "subagents", None) or [])
        try:
            specs = _inject_general_purpose_subagent(
                existing,
                add_general_purpose_agent=True,
                resolved_language=self._resolve_language(),
                rails=getattr(cfg, "rails", None),
                system_prompt=getattr(cfg, "system_prompt", None),
                tools=getattr(cfg, "tools", None),
                mcps=None,
                model=getattr(cfg, "model", None),
                skills=None,
                workspace=getattr(cfg, "workspace", None),
                sys_operation=getattr(cfg, "sys_operation", None),
            )
        except Exception:
            logger.warning(
                "[FlashSubagentRail] general-purpose spec injection failed",
                exc_info=True,
            )
            return False
        if not specs:
            return False
        cfg.subagents = specs
        if len(specs) > len(existing):
            logger.info(
                "[FlashSubagentRail] injected general-purpose subagent spec "
                "(flash-only path, %d existing spec(s) preserved)",
                len(existing),
            )
        return True

    def _build_flash_available_agents(self, subagents) -> str:
        """flash 版 available_agents：general-purpose 用准确的语义化工具面。

        修复描述自相矛盾（正文说继承文件/bash，Tools 括号只列卡片注册的
        web_flash/deepresearch）导致模型误判子代理能力而放弃委派的问题。
        """
        lines = []
        for spec in (subagents or []):
            agent_name, agent_desc = self._extract_agent_meta(spec)
            if agent_name == "general-purpose":
                language = self._resolve_language()
                lines.append(
                    FLASH_AVAILABLE_AGENTS.get(language, FLASH_AVAILABLE_AGENTS["cn"])
                )
            else:
                tools_str = self._extract_agent_tools(spec, agent_name)
                lines.append(f"- {agent_name}: {agent_desc} (Tools: {tools_str})")
        return "\n".join(lines)

    def _apply_flash_descriptions(self, agent) -> None:
        """用 flash 调优文案重写已注册子代理工具的卡描述。

        stock ``create_task_tool`` 构建卡时用的是 agent-core 描述；此处覆盖为
        flash 版（含无条件并行规则与准确的 available_agents 工具面）。
        """
        if not self.tools:
            return
        subagents = getattr(getattr(agent, "deep_config", None), "subagents", None) or []
        available_agents = self._build_flash_available_agents(subagents)
        language = self._resolve_language()
        template = FLASH_TASK_TOOL_DESCRIPTION.get(language, FLASH_TASK_TOOL_DESCRIPTION["cn"])
        rewritten = []
        for tool in self.tools:
            card = getattr(tool, "card", None)
            name = getattr(card, "name", None)
            if name not in _SUBAGENT_TOOL_NAMES:
                continue
            try:
                card.description = template.format(available_agents=available_agents)
                rewritten.append(str(name))
            except (AttributeError, KeyError, IndexError) as exc:
                logger.warning(
                    "[FlashSubagentRail] rewrite card description failed: name=%s err=%s",
                    name,
                    exc,
                )
        if rewritten:
            logger.info(
                "[FlashSubagentRail] flash task_tool descriptions applied: %s",
                ", ".join(rewritten),
            )

    def init(self, agent) -> None:
        """spec 自注入（如需）→ stock 注册 → flash 文案重写。

        注入不做「subagents 为空」前置检查：冷启动带 research/browser/
        自定义 agent 的 flash 会话同样需要 general-purpose（general_agent
        .enabled 是显式 opt-in），助手内部按名去重防重复。
        """
        cfg = getattr(agent, "deep_config", None)
        if self.inject_general_purpose and cfg is not None:
            self._inject_general_purpose_spec(cfg)
        super().init(agent)
        self._apply_flash_descriptions(agent)

    def refresh_available_agents(self, agent) -> None:
        """刷新 available_agents 时保持 flash 文案（不被 stock 描述回写）。

        DisabledToolsRail 会调用本方法同步 disabled_tools 变化后的
        available_agents。先走父类刷新（agent-core 新版会同步
        ``set_allowed_subagent_types``——disabled_tools 变化后的可用子代理
        名单收敛），再用 flash 文案覆写卡描述，兼顾行为同步与文案不回退。
        """
        if not self.tools:
            return
        self.system_prompt_builder = getattr(
            agent, "system_prompt_builder", self.system_prompt_builder
        )
        super().refresh_available_agents(agent)
        self._apply_flash_descriptions(agent)

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        """注入 flash 调优的 task_tool 提示段（替换 stock 强制委派段）。

        async 分支（sessions_spawn）不属 flash 语义，回落 stock 行为。
        """
        if not self.tools or self.system_prompt_builder is None:
            return
        if self.enable_async_subagent:
            await super().before_model_call(ctx)
            return
        language = self._resolve_language()
        content = FLASH_TASK_SECTION.get(language, FLASH_TASK_SECTION["cn"])
        try:
            self.system_prompt_builder.add_section(
                PromptSection(
                    name=SectionName.TASK_TOOL,
                    content={language: content},
                    priority=85,
                )
            )
        except Exception:
            logger.warning(
                "[FlashSubagentRail] inject flash task section failed",
                exc_info=True,
            )
