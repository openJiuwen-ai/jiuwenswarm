# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Prompt templates for Auto Memory extraction.

Based on Claude Code's extractMemories prompts design.
"""

from __future__ import annotations

from typing import Any


def build_extract_memories_prompt(
    new_messages_count: int,
    existing_memories: list[dict[str, Any]],
    language: str = "zh",
) -> str:
    """Build the prompt for memory extraction.

    Args:
        new_messages_count: Number of new messages in this conversation.
        existing_memories: List of existing memory files (from scan_memory_files).
        language: Language for the prompt (zh or en).

    Returns:
        The prompt string.
    """
    if language == "zh":
        return _build_extract_prompt_zh(new_messages_count, existing_memories)
    return _build_extract_prompt_en(new_messages_count, existing_memories)


def _build_extract_prompt_zh(
    new_messages_count: int,
    existing_memories: list[dict[str, Any]],
) -> str:
    """Build Chinese prompt for memory extraction."""
    existing_summary = ""
    if existing_memories:
        existing_summary = "\n现有记忆文件：\n"
        for mem in existing_memories:
            existing_summary += f"- {mem.get('name', 'unknown')}: {mem.get('description', '')}\n"

    return f"""你是一个记忆提取助手。请从对话中提取值得记忆的信息。

## 任务说明

这是一个**系统自动任务**，在对话结束后自动触发。你的职责是自动提取和记录用户偏好、反馈等信息。

对话中有 {new_messages_count} 条新消息。请分析这些消息，提取以下类型的记忆：

1. **user** - 用户画像（角色、偏好、知识）
2. **feedback** - 用户反馈（避免什么、保持什么）
3. **project** - 项目信息（目标、进度、决策）
4. **reference** - 外部资源指针（URLs、dashboards、tickets）

## 重要说明：关于用户意图的理解

⚠️ **特别注意**：
- 如果用户说"让 auto-memory 处理"、"让系统自动处理"或类似表述，这正是**触发你执行此任务的原因**，意味着用户希望你来提取和记录这些信息。
- 不要误解用户的意图。"不要主动记录"是指主 agent 不要在对话中主动记录，而你作为记忆提取子 agent，执行此任务就是用户期望的"auto-memory 处理"。
- 你的任务就是 auto-memory 系统的核心功能，请认真分析对话并提取值得记忆的信息。

## 不要记忆的内容

- 可从代码/git 历史推导的内容
- 临时性信息
- 频繁变化的信息
- 密码、API密钥等敏感信息

{existing_summary}

## 输出要求

**⚠️ 重要：执行效率优化**

你的执行轮次有限（最多 5 轮），请采用高效策略：
- **第 1 轮**：并行读取所有可能需要查看的文件（MEMORY.md、现有记忆文件等）
- **第 2 轮**：并行执行所有写入操作（coding_memory_write 创建新文件、coding_memory_edit 更新现有文件）
- **避免交错读写**：不要在一轮中既读又写，这样会浪费轮次

示例高效流程：
```
Round 1: coding_memory_read(MEMORY.md), coding_memory_read(现有记忆文件)
Round 2: coding_memory_write(新记忆), coding_memory_edit(MEMORY.md 更新索引)
完成！
```

具体操作：

1. 如果发现新的值得记忆的信息：
   - 使用 coding_memory_write 工具创建记忆文件
   - 文件路径：{{语义命名}}.md（如 user_preferences.md）
   - 格式：YAML frontmatter + Markdown 内容
   - 然后使用 coding_memory_edit 工具更新 MEMORY.md 索引

2. 如果现有记忆需要更新：
   - 使用 coding_memory_edit 工具更新对应的记忆文件
   - 如果需要，更新 MEMORY.md 索引

3. 如果没有值得记忆的内容：
   - 不做任何操作

## 记忆文件格式示例

```markdown
---
name: user-preferences
description: User's coding and communication preferences
type: user
---

## 代码风格偏好
- 偏好简洁的代码风格
- 不喜欢过度封装

## 沟通偏好
- 习惯用中文交流
```

## MEMORY.md 索引格式示例

```markdown
- [user] user_preferences.md (2026-06-10T10:30:00): 简洁代码风格，中文交流
```

请开始分析对话内容并提取记忆。"""


def _build_extract_prompt_en(
    new_messages_count: int,
    existing_memories: list[dict[str, Any]],
) -> str:
    """Build English prompt for memory extraction."""
    existing_summary = ""
    if existing_memories:
        existing_summary = "\nExisting memory files:\n"
        for mem in existing_memories:
            existing_summary += f"- {mem.get('name', 'unknown')}: {mem.get('description', '')}\n"

    return f"""You are a memory extraction assistant. Extract memorable information from the conversation.

## Task Description

There are {new_messages_count} new messages in this conversation. Analyze them and extract memories of these types:

1. **user** - User profile (role, preferences, knowledge)
2. **feedback** - User feedback (what to avoid, what to keep)
3. **project** - Project information (goals, progress, decisions)
4. **reference** - External resource pointers (URLs, dashboards, tickets)

## What NOT to remember

- Content derivable from code/git history
- Temporary information
- Frequently changing information
- Passwords, API keys, and other sensitive information

{existing_summary}

## Output Requirements

1. If you find new memorable information:
   - Use coding_memory_write tool to create memory file
   - File path: {semantic-name}.md (e.g., user_role.md)
   - Format: YAML frontmatter + Markdown content
   - Then use coding_memory_edit tool to update MEMORY.md index

2. If existing memories need updates:
   - Use coding_memory_edit tool to update the corresponding memory file
   - Update MEMORY.md index if needed

3. If nothing worth remembering:
   - Do nothing

## Memory File Format Example

```markdown
---
name: user-role
description: User's role and focus
type: user
---

User is a data scientist focused on observability/logging domain.
Prefers log analysis over breakpoint debugging when troubleshooting.
```

## MEMORY.md Index Format Example

```markdown
- [user] user_role.md (2026-06-09T10:30:00): Data scientist focused on observability
```

Please analyze the conversation and extract memories. """


