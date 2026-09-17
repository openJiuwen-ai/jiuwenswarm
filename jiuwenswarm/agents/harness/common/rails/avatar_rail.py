# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""AvatarPromptRail - 数字分身 Rail.

处理所有 per-request 的 avatar 逻辑：
1. before_model_call: 根据 ContextVar 动态注入/移除 avatar 相关 PromptSection
2. before_tool_call: 拦截群聊记忆禁写 + enable_memory=False 场景
"""

from __future__ import annotations

from openjiuwen.core.foundation.llm import ToolMessage
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.common.rails.permissions.owner_scopes import (
    TOOL_PERMISSION_CONTEXT,
)

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

        language = getattr(builder, "language", "cn") or "cn"

        perm_ctx = TOOL_PERMISSION_CONTEXT.get()
        if perm_ctx is None:
            return

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
            disabled_content = _build_memory_fully_disabled_prompt(language)
            section = PromptSection(
                name="memory_fully_disabled",
                content={language: disabled_content},
                priority=_AVATAR_PROMPT_PRIORITY + 2,
            )
            builder.add_section(section)
            self._injected_sections.add("memory_fully_disabled")

        if is_group_digital_avatar:
            interaction_content = _build_interaction_prompt(language)
            section = PromptSection(
                name="interaction_guidance",
                content={language: interaction_content},
                priority=_AVATAR_PROMPT_PRIORITY + 4,
            )
            builder.add_section(section)
            self._injected_sections.add("interaction_guidance")

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
- **定时任务**：可以正常创建并执行 cron 任务和 heartbeat 任务
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


def _build_interaction_prompt(language: str) -> str:
    if language == "cn":
        return """## 多轮交互指引

在以下情况，你必须通过追问来明确需求，不要自行假设或跳过：

### 何时必须追问
1. **缺少关键参数**：任务需要具体参数但用户未提供（如订会议室但没说楼层、时间）
2. **需求模糊或宽泛**：用户请求范围太大或方向不明确，直接执行可能偏离意图（如"帮我写个报告""做个调研""整理一下"）
3. **存在多种理解**：请求可以有多种解读方式，不同理解会导致完全不同的执行结果
4. **需要确认授权**：需要 principal（你代替的人）确认或授权才能执行

### 群聊追问
如果缺少的信息可以由群聊中的某位用户提供，在回复开头加上 `[群聊追问@用户名]`：
- 例：`[群聊追问@张三] 请问需要预约哪个楼层的会议室？`
- 系统会自动在群聊中 @张三 并追踪回复

如果缺少的信息由发送请求的人自己补充即可，在回复开头加上 `[群聊追问]`（不带@）：
- 例：`[群聊追问] 请问会议主题是什么？`
- 例：`[群聊追问] 你说的调研报告是关于哪个方向的？需要覆盖哪些内容？`
- 系统会自动追踪发送者的回复

### 私聊追问
如果需要 principal（你代替的人）确认或授权，在回复开头加上 `[私聊追问]`：
- 例：`[私聊追问] 张三要订会议室，你确认吗？`
- 系统会自动私聊 principal 并在群聊中发送简短确认

### 注意事项
- 需求模糊时**必须追问**，不要自行猜测用户意图后直接执行，否则很可能白做
- 追问时给出具体选项或方向提示，帮助用户快速回复（如"是A方向还是B方向？"而非"你要什么？"）
- 追问前缀必须放在回复的最开头
- 收到追问的回答后，继续完成任务即可，不需要再加前缀
- 收到追问回答后，只针对当前追问的任务继续处理，不要与之前的其他任务混淆
- 如果群聊历史中存在多个不同的任务，务必根据追问上下文区分，只处理当前任务
"""
    return """## Multi-turn Interaction Guidance

You MUST follow up to clarify requirements in these situations — do NOT assume or skip:

### When You Must Follow Up
1. **Missing key parameters**: The task requires specific parameters the user hasn't provided (e.g., booking a room without specifying floor or time)
2. **Vague or broad requests**: The request is too broad or unclear — executing directly may miss the user's intent (e.g., "write a report", "do some research", "organize this")
3. **Ambiguous interpretation**: The request could be understood in multiple ways, leading to very different outcomes
4. **Need confirmation**: You need the principal (the person you represent) to confirm or authorize

### Group Follow-up
If the missing information can be provided by someone in the group chat, prefix your reply with `[群聊追问@Username]`:
- Example: `[群聊追问@张三] Which floor meeting room do you need?`
- The system will automatically @mention the user and track their reply

If the sender can provide the missing information themselves, prefix your reply with `[群聊追问]` (without @):
- Example: `[群聊追问] What is the meeting topic?`
- Example: `[群聊追问] What direction should the research report cover? What topics should it include?`
- The system will automatically track the sender's reply

### DM Follow-up
If you need the principal (the person you represent) to confirm or authorize, prefix your reply with `[私聊追问]`:
- Example: `[私聊追问] 张三 wants to book a meeting room, do you confirm?`
- The system will automatically DM the principal and send a brief acknowledgment in the group

### Notes
- When the request is vague, you **MUST follow up** — do NOT guess the user's intent and execute, or you'll likely waste effort
- When following up, provide specific options or directional hints to help the user reply quickly (e.g., "Direction A or Direction B?" rather than "What do you want?")
- The follow-up prefix must be at the very beginning of your reply
- After receiving the answer, continue completing the task without any prefix
- After receiving the answer, only process the current task from the follow-up, do not mix with previous tasks
- If the group chat history contains multiple different tasks, distinguish them based on the follow-up context and only handle the current one
"""
