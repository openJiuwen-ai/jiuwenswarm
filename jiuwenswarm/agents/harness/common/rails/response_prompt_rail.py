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
_RESPONSE_SECTION_NAMES = (
    "response",
    LocalSectionName.THINKING_DISCIPLINE,
    LocalSectionName.FINAL_VISIBLE_REPLY,
)


def _a2ui_recency_boost_prompt() -> PromptSection:
    """a2ui 路径优先提示，紧邻业务人设(soul, priority 110)之后注入(priority 115)，
    靠 recency 引导浏览器自动化任务走 a2ui-json 表单而非 ask_user 澄清。仅约束执行前的
    任务细节收集/预确认，不冲突 FINAL_VISIBLE_REPLY(120，管工具执行后的最终纯文本轮)。"""
    cn = (
        "# A2UI 路径优先（浏览器自动化任务）\n"
        "当用户请求属于浏览器自动化场景（酒店/机票/门票预订、表单填写、邮箱操作、"
        "社媒发布、在线下单等）且需要收集任务细节或做执行前确认时，**必须优先**直接在回复中"
        "输出一个 `<a2ui-json>...</a2ui-json>` 表单（其 Button 提交 action 名为 "
        "`browser_preflight_submit`）来收集这些信息，**不要**改用 ask_user 工具逐项澄清。"
        "ask_user 仅用于与浏览器自动化无关的一般性澄清。"
        "本条仅约束执行前的任务细节收集与预确认；工具执行完毕后的最终结果仍按"
        "最终可见回复规则用纯文本复述。"
        "若 A2UI 不适用（寒暄、简单事实、纯文本即可），照常纯文本回复，不要强行 A2UI。"
    )
    en = (
        "# A2UI path priority (browser automation tasks)\n"
        "When the user request is a browser-automation scenario (hotel/flight/ticket booking, "
        "form filling, email operations, social posting, online ordering, etc.) and you need to "
        "collect task details or confirm before execution, you **must prefer** to output an "
        "`<a2ui-json>...</a2ui-json>` form (whose Button submit action is "
        "`browser_preflight_submit`) to collect that information, **instead of** using the "
        "ask_user tool to clarify item by item. ask_user is only for general clarifications "
        "unrelated to browser automation. This rule constrains only pre-execution detail "
        "collection and pre-confirmation; the final result after tools finish still follows the "
        "final-visible-reply rule as plain text. If A2UI does not fit (greetings, simple facts, "
        "plain text suffices), answer in plain text; do not force A2UI."
    )
    return PromptSection(
        name=LocalSectionName.A2UI_RECENCY,
        content={"cn": cn, "en": en},
        priority=PromptPriority.A2UI_RECENCY,
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
            self.system_prompt_builder.remove_section(LocalSectionName.A2UI_RECENCY)
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
                is_a2ui_browser_workflow_request(query)
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
            include_browser_workflows=self._is_browser_workflow(ctx),
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
                self.system_prompt_builder.remove_section(LocalSectionName.A2UI_RECENCY)
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
                # recency boost：仅 officeclaw 注入（web 无 request_system_prompt/soul(110)
                # 压顶，a2ui 本就突出不需对冲）。boost priority 115 紧跟 soul(110) 之后、
                # FINAL_VISIBLE_REPLY(120) 之前，靠 recency 对冲 soul 的「结构化输出」引导，
                # 引导浏览器自动化任务走 a2ui-json 而非 ask_user。
                is_officeclaw = str(channel or "").strip().lower() == "officeclaw"
                if is_officeclaw:
                    self.system_prompt_builder.add_section(
                        _a2ui_recency_boost_prompt()
                    )
                    # 诊断日志：直接证据 boost 已注入（priority 115 紧跟 soul 110 之后）
                    logger.info(
                        "[A2UI-BOOST] recency boost injected: request_id=%s "
                        "conversation_id=%s channel=%s priority=%s",
                        request_id or "-",
                        conversation_id or "-",
                        channel or "-",
                        PromptPriority.A2UI_RECENCY,
                    )
                else:
                    self.system_prompt_builder.remove_section(
                        LocalSectionName.A2UI_RECENCY
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
