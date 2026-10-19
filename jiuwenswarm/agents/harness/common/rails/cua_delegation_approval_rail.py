# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Ask the user before the root hands a task to cua_agent.

cua_agent runs inside a synchronous ``task_tool`` delegation, and an interrupt
raised in the child never reaches the conversation: ``task_tool`` keeps only
the child's ``answer`` chunks. Its own ASK rules (``launch_app``,
``type_text``) therefore cannot prompt the user. The approval is taken here
instead, on the root's ``task_tool`` call, where the ordinary confirm
interrupt / resume flow works; once approved, that delegation runs as is.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
from openjiuwen.core.single_agent.interrupt.response import InterruptRequest
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.interrupt.confirm_rail import ConfirmInterruptRail
from openjiuwen.harness.subagents.cua_agent import CUA_PERMISSION_RULES
from openjiuwen.harness.tools.cua.cua_capabilities import (
    DEFAULT_CUA_AGENT_CAPABILITY_NAMES,
    resolve_cua_capabilities,
)

from jiuwenswarm.agents.harness.common.rails.interrupt.permission_options import (
    ALLOW_ONCE,
    REJECT,
    SESSION_ALLOW,
)

CUA_SUBAGENT_TYPE = "cua_agent"
# Distinct from the generic ``task_tool`` key so "remember for this session"
# covers cua delegations only, never every subagent delegation.
CUA_DELEGATION_AUTO_CONFIRM_KEY = "task_tool:cua_agent"

_MAX_TASK_PREVIEW_CHARS = 800

# The actions agent-core marks for approval that cua_agent can actually take.
# jiuwenswarm never selects capabilities, so the agent-core defaults apply.
_ENABLED_CUA_TOOLS = frozenset(
    resolve_cua_capabilities(DEFAULT_CUA_AGENT_CAPABILITY_NAMES).allowed_tool_names
)
GATED_CUA_TOOLS: tuple[str, ...] = tuple(
    tool
    for rule in CUA_PERMISSION_RULES
    for tool in rule["tools"]
    if tool in _ENABLED_CUA_TOOLS
)

_ACTION_LABELS = {
    "cn": {"launch_app": "启动应用", "type_text": "输入文本", "kill_app": "强制关闭应用"},
    "en": {"launch_app": "launch apps", "type_text": "type text", "kill_app": "force-quit apps"},
}

# The first line is all a collapsed card shows, so it names the actions.
_LEAD_CN = "**cua_agent 将操作你的桌面"
_LEAD_EN = "**cua_agent will control your desktop"

_MESSAGE_CN = """\
**cua_agent 将操作你的桌面：{actions}**

任务：
```
{task}
```

cua_agent 使用真实的鼠标键盘操作，批准后才会开始执行。"""

_MESSAGE_EN = """\
**cua_agent will control your desktop: {actions}**

Task:
```
{task}
```

cua_agent uses real mouse and keyboard input and starts only after you approve."""

# Labels stay within the ``permission_options`` aliases: the TUI answers with
# the label, web / CLI with the value, and both must resolve to one action.
# No "always allow": a confirm rail has nothing to persist it to.
_OPTIONS_CN = [
    {"value": ALLOW_ONCE, "label": "本次允许", "description": "仅允许这一次桌面任务"},
    {"value": SESSION_ALLOW, "label": "会话内记住", "description": "本次会话内不再询问 cua_agent 任务"},
    {"value": REJECT, "label": "拒绝", "description": "不执行此桌面任务"},
]
_OPTIONS_EN = [
    {"value": ALLOW_ONCE, "label": "Allow once", "description": "Allow this desktop task only"},
    {"value": SESSION_ALLOW, "label": "Session allow", "description": "Do not ask again for cua_agent tasks this session"},
    {"value": REJECT, "label": "Reject", "description": "Do not run this desktop task"},
]


def is_cua_delegation_approval_message(message: str) -> bool:
    """Return whether ``message`` is this rail's approval copy.

    The interrupt classifier treats any ``task_tool`` interrupt as a permission
    card; this lets it render the cua card as an ordinary confirm instead.
    """
    normalized = (message or "").strip()
    return normalized.startswith(_LEAD_CN) or normalized.startswith(_LEAD_EN)


class CuaDelegationApprovalRail(ConfirmInterruptRail):
    """Confirm ``task_tool`` calls that delegate to cua_agent; pass all others."""

    def __init__(self, language: str = "cn") -> None:
        super().__init__(tool_names=["task_tool"])
        self._language = "en" if language == "en" else "cn"

    def _get_auto_confirm_key(self, tool_call: ToolCall) -> str:
        return CUA_DELEGATION_AUTO_CONFIRM_KEY

    async def resolve_interrupt(
        self,
        ctx: AgentCallbackContext,
        tool_call: Optional[ToolCall],
        user_input: Optional[Any],
        auto_confirm_config: Optional[dict] = None,
    ):
        tool_args = _parse_tool_args(tool_call)
        if str(tool_args.get("subagent_type") or "").strip() != CUA_SUBAGENT_TYPE:
            return self.approve()

        if user_input is None:
            if self._is_auto_confirmed(auto_confirm_config, CUA_DELEGATION_AUTO_CONFIRM_KEY):
                return self.approve()
            return self.interrupt(
                InterruptRequest(
                    message=self._build_message(tool_args.get("task_description")),
                    payload_schema=self.request.payload_schema,
                    auto_confirm_key=CUA_DELEGATION_AUTO_CONFIRM_KEY,
                    ui_options=_OPTIONS_EN if self._language == "en" else _OPTIONS_CN,
                )
            )
        return await super().resolve_interrupt(ctx, tool_call, user_input, auto_confirm_config)

    def _build_message(self, task_description: Any) -> str:
        task = str(task_description or "").strip()
        if len(task) > _MAX_TASK_PREVIEW_CHARS:
            task = task[:_MAX_TASK_PREVIEW_CHARS] + "\n… (truncated)"
        labels = _ACTION_LABELS[self._language]
        separator = ", " if self._language == "en" else "、"
        actions = separator.join(f"{labels.get(tool, tool)} ({tool})" for tool in GATED_CUA_TOOLS)
        if not actions:
            actions = "mouse and keyboard input" if self._language == "en" else "鼠标键盘操作"
        template = _MESSAGE_EN if self._language == "en" else _MESSAGE_CN
        return template.format(actions=actions, task=task)


def _parse_tool_args(tool_call: Optional[ToolCall]) -> dict[str, Any]:
    raw = getattr(tool_call, "arguments", None) if tool_call is not None else None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


__all__ = [
    "CUA_DELEGATION_AUTO_CONFIRM_KEY",
    "CUA_SUBAGENT_TYPE",
    "CuaDelegationApprovalRail",
    "GATED_CUA_TOOLS",
    "is_cua_delegation_approval_message",
]
