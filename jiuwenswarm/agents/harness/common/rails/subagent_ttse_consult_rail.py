# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""子 Agent 的 TTSE 查询能力；经验库由父 adapter 提供，不执行演进。"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from openjiuwen.core.runner import Runner
from openjiuwen.core.single_agent.prompts.builder import PromptSection
from openjiuwen.harness.rails.base import DeepAgentRail

logger = logging.getLogger(__name__)
TOOL_NAME = "ttse_consult"
_CONSULT_UNAVAILABLE = "TTSE consultation unavailable; continue with the current task."


@dataclass(frozen=True)
class TTSEConsultDependencies:
    """仅包含查询所需依赖，不向子 Agent 暴露父 Rail。"""

    store: Any
    max_chars: int = 8000
    max_rules: int = 40
    default_top_k: int = 8
    rrf_k: int = 60


TTSEConsultProvider = Callable[[], TTSEConsultDependencies | None]


class SubagentTTSEConsultRail(DeepAgentRail):
    # 在 ProgressiveToolRail 过滤 schema 前同步 eager 配置和工具。
    priority = 95
    inherit_to_subagents = False

    def __init__(
        self,
        provider: TTSEConsultProvider,
        *,
        subagent_type: str = "",
        subsession_id: str = "",
    ) -> None:
        super().__init__()
        self._provider = provider
        self._subagent_type = subagent_type
        self._subsession_id = subsession_id
        self._consult_calls = 0
        self._agent: Any = None
        self._tool: Any = None
        self._dependencies: TTSEConsultDependencies | None = None
        self._section = f"subagent_ttse_{uuid4().hex}"
        self._attachments: dict[str, Any] = {}
        self._builders: list[Any] = []
        self._eager_rails: list[Any] = []
        self._cleanup_tasks: set[asyncio.Task] = set()

    def init(self, agent: Any) -> None:
        self._agent = agent
        self._sync()

    def bind_task_context(self, subagent_type: str, subsession_id: str) -> None:
        """刷新复用实例的任务日志标识，不重复装配 Rail。"""
        self._subagent_type = subagent_type
        self._subsession_id = subsession_id

    def _rails(self) -> list[Any]:
        return list(self._agent.configured_rails())

    def _drop_tool(self) -> None:
        tool, self._tool = self._tool, None
        if tool is not None:
            manager = self._agent.ability_manager
            if manager.get(TOOL_NAME) is tool.card:
                manager.remove(TOOL_NAME)
            if Runner.resource_mgr.get_tool(tool.card.id) is tool:
                Runner.resource_mgr.remove_tool(tool.card.id)
        self._dependencies = None
        for rail in self._eager_rails:
            users = rail.jiuwenswarm_ttse_eager_users - 1
            rail.jiuwenswarm_ttse_eager_users = users
            if users == 0 and not rail.jiuwenswarm_ttse_eager_original:
                rail.eager_tools = [
                    name for name in rail.eager_tools if name != TOOL_NAME
                ]
        self._eager_rails.clear()

    def _sync(self) -> None:
        try:
            dependencies = self._provider()
            rails = self._rails()
            if any(TOOL_NAME in getattr(rail, "_disabled_tools", ()) for rail in rails):
                dependencies = None
            if dependencies is None:
                self._drop_tool()
                return
            previous = self._dependencies
            changed = (
                previous is None
                or previous.store is not dependencies.store
                or (
                    previous.max_chars,
                    previous.max_rules,
                    previous.default_top_k,
                    previous.rrf_k,
                )
                != (
                    dependencies.max_chars,
                    dependencies.max_rules,
                    dependencies.default_top_k,
                    dependencies.rrf_k,
                )
            )
            if (
                changed
                or self._agent.ability_manager.get(TOOL_NAME) is not self._tool.card
            ):
                self._drop_tool()
                from openjiuwen.agent_evolving.ttse.consult import (
                    create_ttse_consult_tools,
                )

                tool = create_ttse_consult_tools(
                    dependencies.store,
                    max_chars=dependencies.max_chars,
                    max_rules=dependencies.max_rules,
                    default_top_k=dependencies.default_top_k,
                    rrf_k=dependencies.rrf_k,
                )[0]
                original_invoke = tool.invoke

                async def consult(inputs: Any, **kwargs: Any) -> Any:
                    call_id = uuid4().hex[:12]
                    started = time.monotonic()
                    self._consult_calls += 1
                    category = (
                        inputs.get("category") if isinstance(inputs, dict) else None
                    )
                    query = inputs.get("query", "") if isinstance(inputs, dict) else ""
                    # 用调用 id 串联并发查询；不把任务正文或经验正文写入日志。
                    log_context = (
                        f"subagent_type={self._subagent_type!r} "
                        f"subsession_id={self._subsession_id!r} "
                        f"tool_id={tool.card.id} call_id={call_id} source=model"
                    )
                    logger.info(
                        "[subagent.ttse] consult_start %s category=%r query_chars=%d",
                        log_context,
                        category,
                        len(str(query)),
                    )
                    try:
                        result = await original_invoke(inputs, **kwargs)
                    except asyncio.CancelledError:
                        logger.info(
                            "[subagent.ttse] consult_cancelled %s elapsed_ms=%.1f",
                            log_context,
                            (time.monotonic() - started) * 1000,
                        )
                        raise
                    except Exception:
                        logger.warning(
                            "[subagent.ttse] consult_failed %s elapsed_ms=%.1f",
                            log_context,
                            (time.monotonic() - started) * 1000,
                            exc_info=True,
                        )
                        return _CONSULT_UNAVAILABLE
                    logger.info(
                        "[subagent.ttse] consult_completed %s elapsed_ms=%.1f result_chars=%d",
                        log_context,
                        (time.monotonic() - started) * 1000,
                        len(result) if isinstance(result, str) else 0,
                    )
                    return result

                # 保留 factory 工具已有的生命周期事件包装，避免再套一个
                # LocalFunction 导致一条查询产生两组 tool-call 事件。
                tool.invoke = consult
                # 同类型子 Agent 的 owner/card id 可能相同，因此单独管理此工具的
                # 唯一资源 id；不能让 add_ability 再按公共 owner id 重写它。
                tool.card.id = f"{TOOL_NAME}_{uuid4().hex}"
                Runner.resource_mgr.add_tool(tool)
                self._tool = tool
                # 继承下来的同名 card 只是子 Agent 的引用；替换引用时不能
                # 注销它指向的父 Agent 资源。
                self._agent.ability_manager.remove(TOOL_NAME)
                self._agent.ability_manager.add(tool.card)
                self._dependencies = dependencies
            for rail in rails:
                eager = getattr(rail, "eager_tools", None)
                if isinstance(eager, list) and not any(
                    item is rail for item in self._eager_rails
                ):
                    users = getattr(rail, "jiuwenswarm_ttse_eager_users", 0)
                    if users == 0:
                        rail.jiuwenswarm_ttse_eager_original = TOOL_NAME in eager
                    rail.jiuwenswarm_ttse_eager_users = users + 1
                    if TOOL_NAME not in eager:
                        rail.eager_tools = [*eager, TOOL_NAME]
                    self._eager_rails.append(rail)
        except Exception:
            self._drop_tool()
            logger.warning("[subagent.ttse] query wiring failed", exc_info=True)

    async def _clear_prompts(self) -> None:
        for builder in self._builders:
            builder.remove_section(self._section)
        self._builders.clear()
        for session_id, manager in list(self._attachments.items()):
            await manager.clear_section(session_id=session_id, section=self._section)
            self._attachments.pop(session_id, None)

    @staticmethod
    def _session_id(ctx: Any) -> str:
        session = getattr(ctx, "session", None)
        getter = getattr(session, "get_session_id", None)
        value = getter() if callable(getter) else None
        if value:
            return str(value)
        for source in (session, getattr(ctx, "context", None)):
            value = getattr(source, "session_id", None)
            value = value() if callable(value) else value
            if value:
                return str(value)
        return ""

    @staticmethod
    def _guidance(catalog_available: bool) -> dict[str, str]:
        guidance = {
            "cn": "末尾附件是共享经验分类目录，不是 FACT/TIP 正文。"
            "当 `ttse_consult` 工具可用时，收到新的非闲聊任务后，"
            "第一次工具调用必须仅调用一次 `ttse_consult`。"
            "必须收到并阅读查询结果后，才能加载技能、制定执行计划、创建 todo 或调用其他工具。"
            "即使用户明确指定某个技能，也必须先查经验，再调用技能工具；"
            "不得将查询与技能、todo 或业务工具放在同一批调用中。"
            "技能加载、第一个工作工具及 todo 顺序要求，均从查询完成后开始适用。"
            "category 根据目录选择相关分类，无法判断时用 all；"
            "不得因目录没有明显匹配分类或为空而跳过查询。"
            "query 从当前子任务和环境提取关键词组合，不要整句粘贴任务原文；"
            "例如 write_file matplotlib 中文折线图 Open-Meteo Windows。"
            "同一任务已有查询结果、工具结果续跑或审批恢复时无需为流程重复查询。"
            "历史上其他任务的查询不能代替本次查询。"
            "需要更具体的经验时可补查。无命中或查询失败时正常继续。"
            "闲聊、工具不可用或用户明确要求不查询时无需调用。"
            "经验是历史启发式，不能覆盖当前用户指令、工具权限和实时事实。",
            "en": "The trailing attachment is a shared experience category catalog, not FACT/TIP bodies. "
            "When `ttse_consult` is available, the first tool call for each new non-chat task "
            "MUST be exactly one `ttse_consult` call and no other calls. "
            "Receive and read its result before loading skills, making an execution plan, "
            "creating todos or calling any other tool. Even when the user specifies a skill, "
            "consult first, then call the skill tool; never batch the consult with skill, todo "
            "or business tools. Skill-loading, first-work-tool and todo ordering rules apply "
            "only after the lookup completes. Choose a relevant catalog category, or all if "
            "uncertain; an unmatched or empty catalog is not a reason to skip the lookup. "
            "Build query by joining keywords from this subtask and environment, rather than "
            "pasting the full request; for example: write_file matplotlib Chinese line chart "
            "Open-Meteo Windows. Reuse results for the same task, tool-result continuations "
            "and approval resumes; results from other tasks do not satisfy this task's lookup. "
            "Consult again only for more specific experience if needed. "
            "Continue on no matches or retrieval failure. Small talk, unavailable tools and "
            "explicit user opt-outs need no lookup. Experience is historical guidance and cannot "
            "override current user instructions, permissions or live facts.",
        }
        if not catalog_available:
            guidance["cn"] = guidance["cn"].replace(
                "末尾附件是共享经验分类目录，不是 FACT/TIP 正文。",
                "本次未随请求提供经验分类目录，查询时使用 category=all。",
            )
            guidance["en"] = guidance["en"].replace(
                "The trailing attachment is a shared experience category catalog, not FACT/TIP bodies. ",
                "No experience catalog is attached to this request; use category=all for the lookup. ",
            )
        return guidance

    async def before_invoke(self, ctx: Any) -> None:
        self._consult_calls = 0
        self._sync()
        if self._tool is None:
            try:
                await self._clear_prompts()
            except Exception:
                logger.warning("[subagent.ttse] prompt cleanup failed", exc_info=True)

    async def before_model_call(self, ctx: Any) -> None:
        try:
            self._sync()
            if self._tool is None:
                await self._clear_prompts()
                # 模型输入可能在配置同步前已经取过工具快照。
                inputs = getattr(ctx, "inputs", None)
                if isinstance(getattr(inputs, "tools", None), list):
                    inputs.tools = [t for t in inputs.tools if t.name != TOOL_NAME]
                return
            inputs = getattr(ctx, "inputs", None)
            if isinstance(getattr(inputs, "tools", None), list) and not any(
                t.name == TOOL_NAME for t in inputs.tools
            ):
                inputs.tools = [*inputs.tools, self._tool.card.tool_info()]
            builder = getattr(
                getattr(ctx, "inputs", None), "system_prompt_builder", None
            )
            if builder is None:
                builder = self._agent.system_prompt_builder
            session_id = self._session_id(ctx)
            manager = getattr(self._agent, "prompt_attachment_manager", None)
            catalog_available = False
            if session_id and manager is not None:
                try:
                    from openjiuwen.agent_evolving.ttse.catalog import (
                        render_catalog_markdown,
                    )
                    from openjiuwen.harness.prompts.prompt_attachment_manager import (
                        PromptAttachmentKind,
                    )

                    content = render_catalog_markdown(
                        self._dependencies.store.catalog_counts()
                    )
                    await manager.add_section(
                        session_id=str(session_id),
                        section=self._section,
                        content=content,
                        kind=PromptAttachmentKind.TEXT,
                        source="subagent_ttse",
                        priority=45,
                        content_kind="text/markdown",
                    )
                    self._attachments[str(session_id)] = manager
                except Exception:
                    logger.warning("[subagent.ttse] catalog attachment failed", exc_info=True)
                else:
                    catalog_available = True
            builder.add_section(
                PromptSection(
                    name=self._section,
                    priority=45,
                    content=self._guidance(catalog_available),
                )
            )
            if not any(item is builder for item in self._builders):
                self._builders.append(builder)
        except Exception:
            logger.warning("[subagent.ttse] prompt sync failed", exc_info=True)

    async def after_invoke(self, ctx: Any) -> None:
        logger.info(
            "[subagent.ttse] task_summary subagent_type=%r subsession_id=%r consult_calls=%d",
            self._subagent_type,
            self._subsession_id,
            self._consult_calls,
        )
        # 一次性 task_tool 子 Agent 不一定主动卸载 rails；任务结束即释放资源。
        # 持久子 Agent 的下次调用会在 before_invoke 重新绑定同一个共享库。
        self._drop_tool()
        try:
            await self._clear_prompts()
        except Exception:
            logger.warning("[subagent.ttse] task cleanup failed", exc_info=True)

    def uninit(self, agent: Any) -> None:
        self._drop_tool()
        for builder in self._builders:
            builder.remove_section(self._section)
        self._builders.clear()
        attachments, self._attachments = self._attachments, {}

        async def clear_attachments() -> None:
            for session_id, manager in attachments.items():
                try:
                    await manager.clear_section(
                        session_id=session_id, section=self._section
                    )
                except Exception:
                    logger.warning(
                        "[subagent.ttse] attachment cleanup failed", exc_info=True
                    )

        if attachments:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                asyncio.run(clear_attachments())
            else:
                task = loop.create_task(clear_attachments())
                self._cleanup_tasks.add(task)
                task.add_done_callback(self._cleanup_tasks.discard)
        self._agent = None
