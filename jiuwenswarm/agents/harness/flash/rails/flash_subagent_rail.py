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

from openjiuwen.core.common.logging import logger
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts.builder import PromptSection
from openjiuwen.harness.prompts.sections import SectionName
from openjiuwen.harness.rails.subagent.subagent_rail import SubagentRail

from jiuwenswarm.agents.harness.flash.prompt.content.subagent import (
    FLASH_AVAILABLE_AGENTS,
    FLASH_AVAILABLE_AGENTS_CN,
    FLASH_AVAILABLE_AGENTS_EN,
    FLASH_TASK_SECTION,
    FLASH_TASK_SECTION_CN,
    FLASH_TASK_SECTION_EN,
    FLASH_TASK_TOOL_DESCRIPTION,
    FLASH_TASK_TOOL_DESCRIPTION_CN,
    FLASH_TASK_TOOL_DESCRIPTION_EN,
)

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

    def _build_subagent_rails(self):
        """为 gp 子代理构造 flash 变体 rail 集合（CR-2）。

        冷启动时 ``deep_config.rails`` 恒为 None（rails 进 DeepAgent 的
        ``_pending_rails``，不写 deep_config），直接依赖会走 agent-core
        工厂的 stock ``SysOperationRail()`` fallback——gp 子代理获得
        stock 文件工具（含 list_files/powershell），与主代理的 flash
        变体（SlimSysOperationRail，不含这两个）行为不一致。

        这里主动构造 ``SlimSysOperationRail()`` 传入，确保 gp 的文件面
        与主代理一致（FlashReadFileTool/FlashWriteFileTool 等）。构造
        失败时返回 None，工厂 fallback 到 stock（降级可用）。

        刻意不在此挂 ``SubagentPermissionRail``：该 rail 需要
        ``agent_scope_id`` / session，只在 ``create_subagent`` 授权装配时
        才有；冷启动 inject 阶段尚无子会话。主 Agent 专属工具依赖装配期
        ``filter_excluded_spawn_tools``（及 factory inject patch）剔除。
        """
        try:
            from jiuwenswarm.agents.harness.flash import SlimSysOperationRail

            return [SlimSysOperationRail()]
        except Exception:
            logger.warning(
                "[FlashSubagentRail] SlimSysOperationRail construction failed; "
                "gp subagent will fall back to stock SysOperationRail",
                exc_info=True,
            )
            return None

    def _inject_general_purpose_spec(self, cfg) -> bool:
        """向 deep_config.subagents 注入 general-purpose spec（flash 专属路径）。

        复用 agent-core 的注入助手：rails 用 flash 变体（冷启动时
        ``cfg.rails`` 恒 None，不能依赖——改为主动构造 SlimSysOperationRail，
        确保 gp 子代理文件面与主代理一致，见 ``_build_subagent_rails``）；
        tools/model/workspace/sys_operation 均取父配置。传入现有 specs
        （research/browser/自定义 agent 等冷启动产物）而非空列表，由助手
        按名去重——已含 general-purpose（如热重载路径注入过）原样保留，
        未含则插入头部、现有 specs 全部保留。
        """
        from openjiuwen.harness.factory import _inject_general_purpose_subagent

        existing = list(getattr(cfg, "subagents", None) or [])
        # CR-2: cfg.rails 冷启动恒 None（rails 进 _pending_rails 不写
        # deep_config），不能直接依赖——改为主动构造 flash 变体 rail。
        # 热重载路径 cfg.rails 可能有值（_make_deep_agent_config 传入），
        # 优先使用；为空时走 _build_subagent_rails 的 flash fallback。
        cfg_rails = getattr(cfg, "rails", None)
        if not cfg_rails:
            cfg_rails = self._build_subagent_rails()

        from jiuwenswarm.agents.harness.common.tools.subagent_executor.excluded_spawn_tools import (
            filter_excluded_spawn_tools,
        )

        try:
            specs = _inject_general_purpose_subagent(
                existing,
                add_general_purpose_agent=True,
                resolved_language=self._resolve_language(),
                rails=cfg_rails,
                system_prompt=getattr(cfg, "system_prompt", None),
                # 子 Agent 不得继承 send_file_to_user 等主 Agent 专属工具。
                tools=filter_excluded_spawn_tools(getattr(cfg, "tools", None)),
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
                # CR-3: browser_agent 追加 Playwright capabilities 明细，
                # 与 stock SubagentRail._build_available_agents_description 对齐。
                if agent_name == "browser_agent":
                    try:
                        from openjiuwen.harness.tools.browser_move.playwright_runtime.browser_capabilities import (
                            DEFAULT_BROWSER_CAPABILITIES,
                        )

                        lines.append("  Available Playwright capabilities:")
                        for capability in DEFAULT_BROWSER_CAPABILITIES:
                            tool_names = ", ".join(capability.tool_names)
                            lines.append(
                                f"    - {capability.name}: {capability.description} (Tools: {tool_names})"
                            )
                    except ImportError:
                        logger.debug(
                            "[FlashSubagentRail] browser capabilities not available; skipping"
                        )
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
                    priority=440,
                )
            )
        except Exception:
            logger.warning(
                "[FlashSubagentRail] inject flash task section failed",
                exc_info=True,
            )
