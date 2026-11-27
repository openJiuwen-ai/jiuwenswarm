# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""AvatarPromptRail - 数字分身 Rail.

处理所有 per-request 的 avatar 逻辑：
1. before_model_call: 根据 ContextVar 动态注入/移除 avatar 相关 PromptSection
2. before_tool_call: 拦截群聊记忆禁写 + enable_memory=False 场景
"""

from __future__ import annotations

from typing import Any, Optional, Set

from openjiuwen.core.foundation.llm import ToolMessage
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenclaw.agentserver.deep_agent.permissions.owner_scopes import (
    TOOL_PERMISSION_CONTEXT,
    PermissionContext,
)
from jiuwenclaw.agentserver.cron_config import should_register_cron_tools
from jiuwenclaw.utils import logger

_MEMORY_WRITE_TOOLS = frozenset({"write_memory", "edit_memory"})

_AVATAR_PROMPT_PRIORITY = 110


class AvatarPromptRail(DeepAgentRail):
    """数字分身 Rail — 处理所有 per-request 的 avatar 逻辑。

    职责:
    1. before_model_call: 根据 ContextVar 动态注入/移除 avatar 相关 PromptSection
    2. before_tool_call: 拦截群聊记忆禁写 + enable_memory=False 场景
    """

    priority: int = 85

    def __init__(self) -> None:
        super().__init__()
        self._injected_sections: set[str] = set()

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        builder = getattr(
            getattr(self, "_deep_agent", None) or ctx.agent,
            "system_prompt_builder",
            None,
        )
        if builder is None:
            return

        for name in list(self._injected_sections):
            builder.remove_section(name)
        self._injected_sections.clear()

        perm_ctx = TOOL_PERMISSION_CONTEXT.get()
        if perm_ctx is None:
            return

        language = getattr(builder, "language", "cn") or "cn"

        # 数字分身身份提示词（仅群聊数字分身模式）
        if perm_ctx.group_digital_avatar and perm_ctx.avatar_mode:
            display_name = perm_ctx.avatar_principal_name or perm_ctx.principal_user_id
            avatar_content = _build_avatar_prompt(display_name, language)
            section = PromptSection(
                name="avatar_identity",
                content={language: avatar_content},
                priority=_AVATAR_PROMPT_PRIORITY,
            )
            builder.add_section(section)
            self._injected_sections.add("avatar_identity")

        # 判断是否为群聊数字分身模式（三个条件同时满足）
        is_group_digital_avatar = (
            perm_ctx.group_digital_avatar
            and perm_ctx.avatar_mode
        )

        # 群聊数字分身模式：禁止写入记忆
        if is_group_digital_avatar:
            notice = (
                "\n[群聊模式：禁止调用 write_memory/edit_memory]\n"
                if language == "cn"
                else "\n[Group chat mode: write_memory/edit_memory calls are prohibited]\n"
            )
            section = PromptSection(
                name="group_chat_memory_notice",
                content={language: notice},
                priority=_AVATAR_PROMPT_PRIORITY + 1,
            )
            builder.add_section(section)
            self._injected_sections.add("group_chat_memory_notice")

        # 记忆完全禁用（三个条件同时满足：enable_memory=False + group_digital_avatar=True + 群聊消息）
        should_disable_memory = (
            not perm_ctx.enable_memory
            and perm_ctx.group_digital_avatar
            and perm_ctx.avatar_mode
        )
        if should_disable_memory:
            # 使用完全禁用提示词（禁止读取和写入）
            disabled_content = _build_memory_fully_disabled_prompt(language)
            section = PromptSection(
                name="memory_fully_disabled",
                content={language: disabled_content},
                priority=_AVATAR_PROMPT_PRIORITY + 2,
            )
            builder.add_section(section)
            self._injected_sections.add("memory_fully_disabled")

        try:
            from jiuwenclaw.agentserver.memory.forbidden import get_forbidden_memory_prompt
            forbidden = get_forbidden_memory_prompt(language)
            if forbidden:
                section = PromptSection(
                    name="forbidden_memory",
                    content={language: forbidden},
                    priority=_AVATAR_PROMPT_PRIORITY + 3,
                )
                builder.add_section(section)
                self._injected_sections.add("forbidden_memory")
        except Exception as e:
            logger.debug("[AvatarRail] 加载 forbidden_memory 失败: %s", e)

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        """拦截记忆工具调用。

        不依赖 _tool_names 白名单，直接检查所有工具。
        由于 DeepAgentRail.before_tool_call 没有白名单过滤，所有工具调用都会经过这里。

        处理两种场景：
        1. 群聊数字分身模式（group_digital_avatar=True + avatar_mode=True）：禁止写入记忆，但允许读取
        2. 记忆完全禁用（enable_memory=False + group_digital_avatar=True + avatar_mode=True）：禁止读取和写入记忆
        """
        tool_name = ctx.inputs.tool_name
        perm_ctx = TOOL_PERMISSION_CONTEXT.get()
        if perm_ctx is None:
            return

        # 判断是否为群聊数字分身模式
        is_group_digital_avatar = (
            perm_ctx.group_digital_avatar
            and perm_ctx.avatar_mode
        )

        # 判断是否为记忆完全禁用（三个条件同时满足）
        should_disable_memory = (
            not perm_ctx.enable_memory
            and perm_ctx.group_digital_avatar
            and perm_ctx.avatar_mode
        )

        # 场景2：记忆完全禁用 - 禁止读取和写入
        if should_disable_memory:
            all_memory_tools = frozenset({
                "write_memory", "edit_memory", "read_memory", "memory_search", "memory_get"
            })
            if tool_name in all_memory_tools:
                self._reject_tool(ctx, "[PERMISSION_DENIED] 记忆系统已禁用，禁止访问")
            return

        # 场景1：群聊数字分身模式 - 只禁止写入
        if is_group_digital_avatar and tool_name in _MEMORY_WRITE_TOOLS:
            self._reject_tool(ctx, "[PERMISSION_DENIED] 群聊模式下禁止写入/编辑记忆文件")
            return

    @staticmethod
    def _reject_tool(ctx: AgentCallbackContext, message: str) -> None:
        """跳过工具执行，直接返回拒绝消息。"""
        tool_call = ctx.inputs.tool_call
        tool_call_id = tool_call.id if tool_call else ""
        ctx.extra["_skip_tool"] = True
        ctx.inputs.tool_result = message
        ctx.inputs.tool_msg = ToolMessage(content=message, tool_call_id=tool_call_id)


def _build_avatar_prompt(principal_user_id: str | None, language: str) -> str:
    """数字分身身份提示词。文案复用自 agentserver/prompt_builder.py 的 _avatar_prompt()。"""
    if language == "cn":
        if principal_user_id:
            identity = f"你当前正在群聊场景中作为 **{principal_user_id}** 的数字分身发言。"
            perspective = f"1. **第一人称视角**：始终以 {principal_user_id} 的身份和口吻回复，使用\"我\"而非\"他/她\"。"
            boundary = f"2. **承诺边界**：不要代替 {principal_user_id} 做出超出已知信息范围的承诺或决定。如果上下文不足，坦诚说明需要确认，而非编造事实。"
        else:
            identity = "你当前正在群聊场景中作为用户本人的数字分身发言。"
            perspective = "1. **第一人称视角**：始终以用户本人的身份和口吻回复，使用\"我\"而非\"他/她\"。"
            boundary = "2. **承诺边界**：不要代替用户做出超出已知信息范围的承诺或决定。如果上下文不足，坦诚说明需要确认，而非编造事实。"
        cron_capability = (
            "- **定时任务**：可以正常创建并执行 cron 任务和 heartbeat 任务\n"
            if should_register_cron_tools()
            else ""
        )
        return f"""---

# 数字分身模式

{identity}以下规则**仅约束你在群聊中的对外回复方式**，不影响你的任何内部能力。

## 回复风格约束

{perspective}
{boundary}
3. **自然简洁**：回复风格应贴近真人在群聊中的表达习惯，简短、自然，避免过度正式或冗长。
4. **不暴露身份**：不要提及自己是 AI、数字分身或机器人。

## 能力不受影响

数字分身模式**不限制**你的任何 agent 能力，以下功能照常执行：
- **工具调用**：所有工具（文件操作、搜索、代码执行等）正常使用
- **待办管理**：todo_create / todo_complete / todo_insert 等正常使用
{cron_capability.rstrip()}
- **技能调用**：所有已注册技能正常使用
"""
    if principal_user_id:
        identity = f"You are currently acting as **{principal_user_id}**'s digital avatar in a group chat."
        perspective = f"1. **First-person perspective**: Always reply as {principal_user_id},\
             using \"I\" instead of \"he/she\"."
        boundary = f"2. **Commitment boundary**: Do not make commitments or decisions beyond \
            known information on behalf of {principal_user_id}."
    else:
        identity = "You are currently acting as the user's digital avatar in a group chat."
        perspective = "1. **First-person perspective**: Always reply as the user, using \"I\" instead of \"he/she\"."
        boundary = "2. **Commitment boundary**: Do not make commitments or decisions beyond known \
            information on behalf of the user."
    return f"""---

# Digital Avatar Mode

{identity} The rules below **only constrain your outward reply style** in group chat.

## Reply Style Constraints

{perspective}
{boundary}
3. **Natural and concise**: Reply style should resemble a real person's expression in group chat.
4. **Do not reveal identity**: Never mention that you are an AI, digital avatar, or bot.
"""


def _build_memory_disabled_prompt(language: str) -> str:
    """记忆写入禁用提示词（保留读能力，与 React 链路行为一致）。"""
    if language == "cn":
        return """## 记忆系统 - 写入已禁用

**记忆写入功能当前已禁用。**

- **禁止** 使用 write_memory、edit_memory 写入或修改记忆文件
- **允许** 使用 memory_search、memory_get、read_memory 查询已有记忆
- 如果用户要求记住某些内容，回复："记忆写入功能当前未启用，无法保存新信息，但我可以查询已有的记忆。"
"""
    return """## Memory System - Write Disabled

**Memory write operations are currently disabled.**

- **Do NOT** use write_memory or edit_memory to write or modify memory files
- **Allowed**: memory_search, memory_get, read_memory for reading existing memories
- If the user asks to remember something, reply: "Memory writing is currently disabled, but I can query existing memories."
"""


def _build_memory_fully_disabled_prompt(language: str) -> str:
    """记忆完全禁用提示词（禁止读取和写入）。"""
    if language == "cn":
        return """## 记忆系统 - 已完全禁用

**记忆系统当前已完全禁用。**

- **禁止** 使用任何记忆工具：
  - 写入工具：write_memory、edit_memory
  - 读取工具：read_memory、memory_search、memory_get
- 如果用户询问历史信息或要求记住某些内容，回复："记忆系统当前已禁用，我无法访问历史记录或保存新信息。"
"""
    return """## Memory System - Fully Disabled

**The memory system is currently fully disabled.**

- **Do NOT** use any memory tools:
  - Write tools: write_memory, edit_memory
  - Read tools: read_memory, memory_search, memory_get
- If the user asks about historical information or requests to remember something, reply: \
    "The memory system is currently disabled. I cannot access historical records or save new information."
"""


__all__ = [
    "AvatarPromptRail",
]
