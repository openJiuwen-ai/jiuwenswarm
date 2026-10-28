# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Inject the response/message-format section before each model call."""

from __future__ import annotations

import logging

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.common.context_keys import JIUWENSWARM_CHANNEL_CONTEXT_KEY
from jiuwenswarm.agents.harness.common.prompt.prompt_builder import (
    LocalSectionName,
    PromptPriority,
    _final_visible_reply_prompt,
    _response_prompt,
    _thinking_discipline_prompt,
)
from jiuwenswarm.server.runtime.a2ui.prompt_instructions import (
    is_a2ui_browser_workflow_request,
)

logger = logging.getLogger(__name__)

SKIP_A2UI_PROMPT_CONTEXT_KEY = "skip_a2ui"
A2UI_BROWSER_WORKFLOW_CONTEXT_KEY = "a2ui_browser_workflow"
# browser-workflow 功能总开关（休眠中）：False 时 a2ui prompt 不注入
# preflight/发送确认门禁规则，browser 意图检测被旁路。
# 重开前需：① 修复 browser-workflow prompt 的已知问题（见 PR #8390 检视记录）；
# ② 与工具注册联动——同时设置 env PLAYWRIGHT_RUNTIME_MCP_ENABLED，否则只开
# prompt 无工具；只开 env 不开本开关则模型可执行浏览器动作但缺确认门禁提示。
# 两处 call-site（before_invoke / before_model_call）均引用本常量，恢复只改这一处。
_BROWSER_WORKFLOW_ENABLED = False
_RESPONSE_SECTION_NAMES = (
    "response",
    LocalSectionName.THINKING_DISCIPLINE,
    LocalSectionName.FINAL_VISIBLE_REPLY,
)


def _a2ui_boost_prompt() -> PromptSection:
    """a2ui 强化提示：priority 117（THINKING_DISCIPLINE 115 之后、FINAL_VISIBLE_REPLY 120 之前），
    强化 a2ui 触发倾向、抵消 soul 结构化输出对 a2ui 的稀释——交互场景必须输出 a2ui-json、
    禁止 markdown 模拟表单。只豁免最终纯文本总结轮（FINAL_VISIBLE_REPLY(120) 管的那一轮），
    中间任何需要用户输入/选择的轮次（含工具执行后用 a2ui 呈现结果让用户挑选）都必须出 a2ui-json。"""
    cn = (
        "# A2UI 是交互场景的唯一正确输出格式\n"
        "当用户的请求需要交互式收集信息（如选择、输入、确认等），"
        "或需要展示 2 个及以上同类项的结构化对比，"
        "必须输出合法的 `<a2ui-json>` 块。"
        "不要用 markdown 列表、纯文字选项列表或伪按钮文本模拟表单——"
        "markdown 模拟表单不可交互，a2ui 可用时禁止使用。"
        "本规则只豁免最终纯文本总结轮（工具/任务全部完成后的收尾回复）；"
        "中间轮次只要需要用户提供输入或选择（包括工具搜索出结果后让用户从中挑选），"
        "都必须用 a2ui-json。"
    )
    en = (
        "# A2UI is the only correct output format for interactive scenarios\n"
        "When the request requires interactive information collection "
        "(e.g., selecting, inputting, or confirming), or requires displaying "
        "a structured comparison of 2+ same-type items, "
        "you MUST output a valid `<a2ui-json>` block. "
        "Do NOT use markdown lists, plain-text option lists, or pseudo-button "
        "text to simulate forms — markdown mock forms are not interactive and "
        "are forbidden when a2ui is available. "
        "This rule exempts ONLY the final plain-text wrap-up turn (after all "
        "tools/tasks complete). In any intermediate turn where the user must "
        "provide input or make a choice — including picking among tool search "
        "results — you must use a2ui-json."
    )
    return PromptSection(
        name=LocalSectionName.A2UI_BOOST,
        content={"cn": cn, "en": en},
        priority=PromptPriority.A2UI_BOOST,
    )


class ResponsePromptRail(DeepAgentRail):
    """Inject response format, thinking discipline, and final-reply rules."""

    priority = 5

    def __init__(self) -> None:
        super().__init__()
        self.system_prompt_builder = None
        self._runtime_channel: str | None = None

    def init(self, agent) -> None:
        self.system_prompt_builder = getattr(agent, "system_prompt_builder", None)

    def uninit(self, agent) -> None:
        if self.system_prompt_builder is not None:
            for name in _RESPONSE_SECTION_NAMES:
                self.system_prompt_builder.remove_section(name)
            self.system_prompt_builder.remove_section(LocalSectionName.A2UI)
            self.system_prompt_builder.remove_section(LocalSectionName.A2UI_BOOST)
        self.system_prompt_builder = None
        self._runtime_channel = None

    def set_channel(self, channel: str | None) -> None:
        value = str(channel or "").strip()
        self._runtime_channel = value or None

    async def before_invoke(self, ctx: AgentCallbackContext) -> None:
        channel = self._resolve_channel(ctx)
        extra = getattr(ctx, "extra", None)
        if isinstance(extra, dict) and channel:
            extra[JIUWENSWARM_CHANNEL_CONTEXT_KEY] = channel
        if isinstance(extra, dict) and self._should_skip_a2ui(ctx):
            extra[SKIP_A2UI_PROMPT_CONTEXT_KEY] = True
        if isinstance(extra, dict):
            inputs = getattr(ctx, "inputs", None)
            query = (
                inputs.get("query")
                if isinstance(inputs, dict)
                else getattr(inputs, "query", None)
            )
            extra[A2UI_BROWSER_WORKFLOW_CONTEXT_KEY] = (
                _BROWSER_WORKFLOW_ENABLED and is_a2ui_browser_workflow_request(query)
            )

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        if self.system_prompt_builder is None:
            return

        language = self.system_prompt_builder.language or "cn"
        self.system_prompt_builder.add_section(_response_prompt(language))
        self.system_prompt_builder.add_section(_thinking_discipline_prompt(language))
        self.system_prompt_builder.add_section(_final_visible_reply_prompt(language))
        self._sync_a2ui_prompt_section(
            self._resolve_channel(ctx),
            skip_a2ui=self._should_skip_a2ui(ctx),
            include_browser_workflows=_BROWSER_WORKFLOW_ENABLED and self._is_browser_workflow(ctx),
            identity=(
                self._resolve_context_value(ctx, "request_id"),
                (
                    self._resolve_context_value(ctx, "conversation_id")
                    or self._resolve_context_value(ctx, "session_id")
                ),
            ),
        )

    def _should_skip_a2ui(self, ctx: AgentCallbackContext) -> bool:
        inputs = getattr(ctx, "inputs", None)
        if isinstance(inputs, dict) and inputs.get(SKIP_A2UI_PROMPT_CONTEXT_KEY) is True:
            return True
        if getattr(inputs, SKIP_A2UI_PROMPT_CONTEXT_KEY, False) is True:
            return True

        extra = getattr(ctx, "extra", None)
        return isinstance(extra, dict) and extra.get(SKIP_A2UI_PROMPT_CONTEXT_KEY) is True

    def _resolve_channel(self, ctx: AgentCallbackContext) -> str | None:
        """Read the request channel from callback inputs when available."""
        inputs = getattr(ctx, "inputs", None)
        if isinstance(inputs, dict):
            value = inputs.get("channel")
            if value is not None:
                return str(value)
        else:
            value = getattr(inputs, "channel", None)
            if value is not None:
                return str(value)

        extra = getattr(ctx, "extra", None)
        if isinstance(extra, dict):
            value = extra.get(JIUWENSWARM_CHANNEL_CONTEXT_KEY)
            if value is not None:
                return str(value)

        if self._runtime_channel is not None:
            return self._runtime_channel

        conversation_id = (
            inputs.get("conversation_id")
            if isinstance(inputs, dict)
            else getattr(inputs, "conversation_id", None)
        )
        if isinstance(conversation_id, str) and "_" in conversation_id:
            channel = conversation_id.split("_", 1)[0]
            if channel == "sess":
                return "web"
            if channel:
                return channel

        return None

    @staticmethod
    def _resolve_context_value(ctx: AgentCallbackContext, key: str) -> str | None:
        for source in (getattr(ctx, "inputs", None), getattr(ctx, "extra", None)):
            value = source.get(key) if isinstance(source, dict) else getattr(source, key, None)
            if value is not None and str(value).strip():
                return str(value)
        return None

    def _is_browser_workflow(self, ctx: AgentCallbackContext) -> bool:
        extra = getattr(ctx, "extra", None)
        if isinstance(extra, dict) and A2UI_BROWSER_WORKFLOW_CONTEXT_KEY in extra:
            return extra[A2UI_BROWSER_WORKFLOW_CONTEXT_KEY] is True

        inputs = getattr(ctx, "inputs", None)
        query = inputs.get("query") if isinstance(inputs, dict) else getattr(inputs, "query", None)
        return is_a2ui_browser_workflow_request(query)

    def _sync_a2ui_prompt_section(
        self,
        channel: str | None,
        *,
        skip_a2ui: bool = False,
        include_browser_workflows: bool = False,
        identity: tuple[str | None, str | None] = (None, None),
    ) -> None:
        """Inject or remove the A2UI prompt section from runtime config."""
        if self.system_prompt_builder is None:
            return

        request_id, conversation_id = identity
        try:
            from jiuwenswarm.server.runtime.a2ui.integration import is_a2ui_channel
            from jiuwenswarm.server.runtime.a2ui.config import get_current_a2ui_config
            from jiuwenswarm.server.runtime.a2ui.runtime.prompt import build_a2ui_prompt_section

            is_web_channel = is_a2ui_channel(channel)
            enabled = get_current_a2ui_config().enabled if is_web_channel else False
            injected = is_web_channel and enabled and not skip_a2ui

            if not injected:
                self.system_prompt_builder.remove_section(LocalSectionName.A2UI)
                self.system_prompt_builder.remove_section(LocalSectionName.A2UI_BOOST)
            else:
                self.system_prompt_builder.add_section(
                    PromptSection(
                        name=LocalSectionName.A2UI,
                        content={
                            "cn": build_a2ui_prompt_section(
                                "cn",
                                include_browser_workflows=include_browser_workflows,
                            ),
                            "en": build_a2ui_prompt_section(
                                "en",
                                include_browser_workflows=include_browser_workflows,
                            ),
                        },
                        priority=PromptPriority.A2UI,
                    )
                )
                # a2ui 强化(boost)：仅 officeclaw 注入（officeclaw 的 system prompt 含
                # 性格描述，会引导模型走结构化输出而非 a2ui，需对冲）。priority 117
                # （THINKING_DISCIPLINE 115 之后、FINAL_VISIBLE_REPLY 120 之前），
                # 引导交互场景走 a2ui-json 而非 ask_user/markdown 模拟。
                is_officeclaw = str(channel or "").strip().lower() == "officeclaw"
                if is_officeclaw:
                    self.system_prompt_builder.add_section(
                        _a2ui_boost_prompt()
                    )
                else:
                    self.system_prompt_builder.remove_section(
                        LocalSectionName.A2UI_BOOST
                    )

            logger.info(
                "A2UI prompt decision: request_id=%s conversation_id=%s "
                "channel=%s enabled=%s injected=%s browser_workflows=%s skip_a2ui=%s",
                request_id or "-",
                conversation_id or "-",
                channel or "-",
                enabled,
                injected,
                include_browser_workflows if injected else False,
                skip_a2ui,
            )
        except Exception:
            logger.exception(
                "Failed to sync A2UI prompt section: request_id=%s "
                "conversation_id=%s channel=%s",
                request_id or "-",
                conversation_id or "-",
                channel or "-",
            )
            self.system_prompt_builder.remove_section(LocalSectionName.A2UI)
