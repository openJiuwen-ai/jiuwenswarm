# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""FlashMemoryRail — MemoryRail 的 flash 统一工具面变体。

差异（相对 stock MemoryRail）：
- 工具面 5→1：``_register_memory_tools`` 只注册 :class:`FlashMemoryTool`
  （单卡 ``memory``，mode 分发 write/edit/read/search），替换 stock 五件套；
- 读保护双源：cron/heartbeat 只读（基类 ``_is_read_only``）OR 群聊数字分身
  只读（``_group_read_only``，由 ``_update_session_tools`` 经 ``set_read_only``
  切换——统一工具读写同卡，无法像 stock 那样按名单移除写工具）；
- 提示词工具名改写：stock 记忆/每日记忆文案按五件套措辞（memory_search 等）
  编写，注入前改写为统一入口形式（memory(mode=...)）。
"""

from __future__ import annotations

import logging

from openjiuwen.core.foundation.store.base_embedding import EmbeddingConfig
from openjiuwen.core.memory.lite.config import create_memory_settings
from openjiuwen.core.memory.lite.memory_tool_context import MemoryToolContext
from openjiuwen.harness.prompts.sections.memory import build_memory_section
from openjiuwen.harness.rails import MemoryRail

from ..tools.flash_memory_tool import FlashMemoryTool

logger = logging.getLogger(__name__)


class FlashMemoryRail(MemoryRail):
    """flash 统一 memory 工具面（MemoryRail 子类，单卡 mode 分发）。"""

    # stock 记忆文案中的五件套工具名 → 统一入口形式
    _MEMORY_NAME_REPLACEMENTS = (
        ("memory_search", "memory(mode=search)"),
        ("memory_get", "memory(mode=read)"),
        ("read_memory", "memory(mode=read)"),
        ("write_memory", "memory(mode=write)"),
        ("edit_memory", "memory(mode=edit)"),
    )

    # 内容松绑：deep 原样正文里的「每轮强制检索」→「任务开始检索一次」。
    # 整块改写：标题、前置强制句、禁止跳过句、每轮第一步句四处一起改，
    # 否则同一段里会同时出现「一次即可」与「任何问题前必须检索」的相反规则。
    # 注意：内容替换在工具名替换之后执行，锚点须用统一入口后的 ``memory(mode=search)``。
    # 与工具名替换同序执行（先 5→1 再内容），命中守卫分别检测。
    _MEMORY_CONTENT_REPLACEMENTS_CN = (
        ("## 强制检索（最高优先级）", "## 主动检索（任务开始时优先）"),
        (
            "在开始执行任何任务、回答任何问题之前，必须先调用 `memory(mode=search)` 检索工作区记忆",
            "在开始一个新任务之前，先调用 `memory(mode=search)` 检索工作区记忆",
        ),
        (
            "**禁止跳过检索**：即使问题看起来很简单、你自认为已经知道答案，也必须先检索。",
            "**不要凭猜测跳过检索**：当任务可能涉及用户偏好、约定、约束或历史决策时，先检索。",
        ),
        (
            "是每轮回答前的固定第一步，没有任何例外",
            "在任务开始时做一次，之后仅在需要新的用户上下文时再做",
        ),
    )

    _MEMORY_CONTENT_REPLACEMENTS_EN = (
        ("## Mandatory Retrieval (Highest Priority)", "## Proactive Retrieval (Prefer at Task Start)"),
        (
            "Before starting any task or answering any question, you MUST first call "
            "`memory(mode=search)` to retrieve workspace memory",
            "Before starting a new task, first call "
            "`memory(mode=search)` to retrieve workspace memory",
        ),
        (
            "**Never skip the search**: even if the question seems simple or you think "
            "you already know the answer, you must search first.",
            "**Do not skip the search on a guess**: when a task may involve user "
            "preferences, conventions, constraints, or past decisions, search first.",
        ),
        (
            "is the mandatory first step before every answer, with no exceptions",
            "should be done once at task start, then only when new user context is needed",
        ),
    )

    def __init__(
        self,
        embedding_config: EmbeddingConfig,
        is_proactive: bool = True,
    ) -> None:
        super().__init__(embedding_config=embedding_config, is_proactive=is_proactive)
        self._group_read_only = False
        self._unified_tool: FlashMemoryTool | None = None

    def set_read_only(self, value: bool) -> None:
        """外部场景（群聊数字分身）切换只读；与 cron/heartbeat 只读 OR 生效。"""
        self._group_read_only = bool(value)

    def _read_only_now(self) -> bool:
        return bool(self._is_read_only or self._group_read_only)

    def _register_memory_tools(self, agent) -> None:
        if not hasattr(agent, "ability_manager"):
            logger.warning("[FlashMemoryRail] Agent has no ability_manager")
            return

        try:
            agent_id = getattr(getattr(agent, "card", None), "id", None) or "default"
            language = getattr(self.system_prompt_builder, "language", "cn")

            memory_dir = str(self.workspace.get_node_path("memory") or "") if self.workspace else ""
            settings = create_memory_settings(memory_dir)
            self._tool_ctx = MemoryToolContext(
                workspace=self.workspace,
                settings=settings,
                agent_id=agent_id,
                embedding_config=self._embedding_config,
                sys_operation=self.sys_operation,
                manager=None,
                node_name="memory",
            )

            tool = FlashMemoryTool(
                self._tool_ctx,
                read_only_flag=self._read_only_now,
                language=language,
                agent_id=agent_id,
            )
            result = agent.ability_manager.add_ability(tool.card, tool)
            if result.added:
                self._owned_tool_cards[tool.card.name] = tool.card
                self._unified_tool = tool
                logger.info("[FlashMemoryRail] Registered tool: %s", tool.card.name)
        except Exception as exc:
            logger.error("[FlashMemoryRail] Failed to register memory tools: %s", exc)

    def uninit(self, agent) -> None:
        super().uninit(agent)
        self._unified_tool = None

    def restore_memory_tool(self, agent) -> None:
        """记忆完全禁用场景按名移除过统一工具后，恢复分支从这里重新注册。"""
        tool = self._unified_tool
        if tool is None:
            return
        ability_manager = getattr(agent, "ability_manager", None)
        if ability_manager is None:
            return
        if ability_manager.get(tool.card.name) is not None:
            return
        try:
            ability_manager.add_ability(tool.card, tool)
            logger.info("[FlashMemoryRail] Re-registered tool: %s", tool.card.name)
        except Exception as exc:
            logger.warning("[FlashMemoryRail] Re-register tool failed: %s", exc)

    async def before_model_call(self, ctx) -> None:
        """与 MemoryRail.before_model_call 保持同步（升级 agent-core 时回归）。

        差异仅两点：read_only 取双源 OR 结果；记忆/每日记忆文案里的工具名
        改写为统一入口（memory(mode=...)）。
        """
        if self.system_prompt_builder is None:
            return

        self.system_prompt_builder.remove_section("memory")
        memory_section = build_memory_section(
            language=self.system_prompt_builder.language,
            read_only=self._read_only_now(),
            is_proactive=self._is_proactive,
        )
        if memory_section is not None:
            self.system_prompt_builder.add_section(
                self._rewrite_section(memory_section)
            )

        daily_content = await self._load_recent_daily_memory()
        if daily_content:
            from openjiuwen.harness.prompts.builder import PromptSection

            lang = self.system_prompt_builder.language or "cn"
            if lang == "cn":
                header = (
                    "# 今日与昨日记忆（已自动加载）\n\n"
                    "以下记忆已自动加载，无需调用 memory(mode=read) "
                    "来获取今日/昨日记录。\n\n"
                )
            else:
                header = (
                    "# Today's and Yesterday's Memory (auto-loaded)\n\n"
                    "The following memories are auto-loaded. "
                    "Do not call memory(mode=read) "
                    "to retrieve today's/yesterday's records.\n\n"
                )
            dm_section = PromptSection(
                name="daily_memory_context",
                content={lang: header + daily_content},
                priority=52,
            )
            self.system_prompt_builder.add_section(dm_section)

    def _rewrite_section(self, section):
        """把 section 文案里的五件套工具名改写为统一入口形式（带命中守卫）。"""
        content = getattr(section, "content", None)
        if not isinstance(content, dict):
            return section
        new_content = {}
        for lang_key, text in content.items():
            if isinstance(text, str):
                original = text
                for old, new in self._MEMORY_NAME_REPLACEMENTS:
                    text = text.replace(old, new)
                names_missed = text == original
                pairs = (
                    self._MEMORY_CONTENT_REPLACEMENTS_CN
                    if lang_key in ("cn", "zh")
                    else self._MEMORY_CONTENT_REPLACEMENTS_EN
                )
                hits = 0
                for old, new in pairs:
                    if old in text:
                        text = text.replace(old, new)
                        hits += 1
                # 命中守卫：两套替换分别检测。上游只改其中一套时，合并检测会把
                # 「工具名命中、检索句漂移」的半失效情形静默放过；整块松绑只命中
                # 一部分时同样要告警——否则会留下互相矛盾的强制检索规则。
                if names_missed:
                    logger.warning(
                        "[FlashMemoryRail] 记忆文案未命中五件套工具名（%s 段），"
                        "上游可能改了文案——统一名改写失效",
                        lang_key,
                    )
                if hits == 0:
                    logger.warning(
                        "[FlashMemoryRail] 记忆文案未命中强制检索块（%s 段），"
                        "上游可能改了文案——内容松绑失效",
                        lang_key,
                    )
                elif hits < len(pairs):
                    logger.warning(
                        "[FlashMemoryRail] 记忆强制检索块仅命中 %d/%d 处（%s 段），"
                        "上游改了部分文案——松绑后可能残留相反规则，请同步锚点",
                        hits, len(pairs), lang_key,
                    )
            new_content[lang_key] = text
        try:
            return type(section)(
                name=section.name,
                content=new_content,
                priority=section.priority,
            )
        except Exception:
            return section


__all__ = ["FlashMemoryRail"]
