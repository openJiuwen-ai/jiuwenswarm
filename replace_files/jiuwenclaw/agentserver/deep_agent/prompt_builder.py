# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
from datetime import datetime, timezone, timedelta
from enum import IntEnum
from pathlib import Path
from typing import Optional
import sys

from openjiuwen.harness.prompts import SystemPromptBuilder, PromptSection, resolve_language
from jiuwenclaw.agentserver.cron_config import should_register_cron_tools
from jiuwenclaw.utils import logger, get_agent_root_dir


def _should_show_cron_tools() -> bool:
    return should_register_cron_tools()


class PromptPriority(IntEnum):
    """Named prompt section priorities for local builder sections."""

    IDENTITY = 10
    SAFETY = 20
    TOOLS = 30
    SKILLS = 40
    SKILL_PROTOCOL = 45
    MEMORY = 50
    RESPONSE = 60
    WORKSPACE = 70
    TODO = 85


def _response_prompt(language: str) -> PromptSection:
    if _should_show_cron_tools():
        zh_system_type = "【cron 或 heartbeat 或 notify】"
        zh_cron_note = "- **cron**：定时任务，如「每日提醒」「周报汇总」。\n"
        en_system_type = "【cron or heartbeat or notify】"
        en_cron_note = '- **cron**: Scheduled tasks, e.g. "daily reminder", "weekly summary".\n'
    else:
        zh_system_type = "【heartbeat 或 notify】"
        zh_cron_note = ""
        en_system_type = "【heartbeat or notify】"
        en_cron_note = ""

    if language == "cn":
        content = f"""# 消息说明

你会收到用户消息和系统消息，需按来源和类型分别处理。

## 用户消息

```json
{{
  "channel": "【频道来源，如 feishu / telegram / web】",
  "preferred_response_language": "【en 或 zh】",
  "content": "【用户消息内容】",
  "source": "user"
}}
```

## 系统消息

```json
{{
  "type": "{zh_system_type}",
  "preferred_response_language": "【en 或 zh】",
  "content": "【任务信息】",
  "source": "system"
}}
```

{zh_cron_note}- **heartbeat**：心跳任务，如「检查待办」「同步状态」。

系统任务完成后，以回复形式通知用户。
"""
    else:
        content = f"""# Message Format

You receive user messages and system messages; handle each by source and type.

## User Message

```json
{{
  "channel": "【channel source, e.g. feishu / telegram / web】",
  "preferred_response_language": "【en or zh】",
  "content": "【user message content】",
  "source": "user"
}}
```

## System Message

```json
{{
  "type": "{en_system_type}",
  "preferred_response_language": "【en or zh】",
  "content": "【task info】",
  "source": "system"
}}
```

{en_cron_note}- **heartbeat**: Heartbeat tasks, e.g. "check todos", "sync status".

After completing a system task, notify the user via a reply.
"""
    return PromptSection(
        name="response",
        content={language: content},
        priority=PromptPriority.RESPONSE,
    )


def _runtime_environment_prompt(language: str) -> str:
    """OS / shell command hints shared by main agent identity and subagent / fork / spawn base prompts."""
    os_type = sys.platform
    if language == "cn":
        return f"""## 运行环境

当前运行平台：`{os_type}`

**重要提示**：必须严格使用与当前平台匹配的命令语法，切勿使用其他平台的命令格式。

常见命令差异对照：

| 操作 | Windows (`win32`/`win64`) | Linux/macOS (`linux`/`darwin`) |
|------|---------------------------|-------------------------------|
| 创建目录 | `mkdir folder` 或 PowerShell `New-Item -ItemType Directory -Path folder` | `mkdir -p folder` |
| 查看文件 | `type file.txt` 或 PowerShell `Get-Content file.txt` | `cat file.txt` |
| 列出文件 | `dir` 或 PowerShell `Get-ChildItem` | `ls -la` |
| 删除文件 | `del file.txt` 或 PowerShell `Remove-Item file.txt` | `rm file.txt` |
| 删除目录 | `rmdir folder` 或 PowerShell `Remove-Item -Recurse folder` | `rm -rf folder` |
| 查找文件 | `dir /s pattern` 或 PowerShell `Get-ChildItem -Recurse -Filter pattern` | `find . -name pattern` |

**特别注意**：Windows 的 `mkdir` 不支持 `-p` 参数！在 Windows 上使用 `mkdir -p folder` 会错误创建名为 `-p` 的目录。如需创建嵌套目录，请使用 PowerShell `New-Item -ItemType Directory -Path "parent/child" -Force`，或使用 cmd 分步创建 `mkdir parent && mkdir parent\\child`。
"""
    return f"""## Runtime Environment

Current platform: `{os_type}`

**Important**: You MUST strictly use command syntax matching the current platform. Never use command formats from other platforms.

Common command differences:

| Operation | Windows (`win32`/`win64`) | Linux/macOS (`linux`/`darwin`) |
|-----------|---------------------------|-------------------------------|
| Create directory | `mkdir folder` or PowerShell `New-Item -ItemType Directory -Path folder` | `mkdir -p folder` |
| View file | `type file.txt` or PowerShell `Get-Content file.txt` | `cat file.txt` |
| List files | `dir` or PowerShell `Get-ChildItem` | `ls -la` |
| Delete file | `del file.txt` or PowerShell `Remove-Item file.txt` | `rm file.txt` |
| Delete directory | `rmdir folder` or PowerShell `Remove-Item -Recurse folder` | `rm -rf folder` |
| Find file | `dir /s pattern` or PowerShell `Get-ChildItem -Recurse -Filter pattern` | `find . -name pattern` |

**WARNING**: Windows `mkdir` does NOT support the `-p` flag! Using `mkdir -p folder` on Windows will incorrectly create a directory named `-p`. To create nested directories on Windows, use either PowerShell `New-Item -ItemType Directory -Path "parent/child" -Force` or cmd with step-by-step creation `mkdir parent && mkdir parent\\child`.
"""


def _identity_prompt(language: str) -> PromptSection:
    if language == "cn":
        content = f"""你是一个私人智能体。像一个有温度的人类助手一样与用户互动。

对外交流时，不要主动提及内部框架名、内部目录名、供应商实现或运行细节；如果上层系统已经定义了你的对外身份、产品名称或自我介绍口径，应以该口径为准，不要补充内部实现信息。

---

{_runtime_environment_prompt("cn")}
"""
    else:
        content = f"""
You are a personal agent. Interact with your user like a warm, human-like assistant.

When talking to the user, do not proactively mention internal framework names, internal directory names, vendor implementation details, or runtime details. If the host system has already defined your external identity, product name, or self-introduction, follow that wording and do not add internal implementation details.

---

{_runtime_environment_prompt("en")}
"""
    return PromptSection(
        name="identity",
        content={language: content},
        priority=PromptPriority.IDENTITY,
    )


def build_identity_prompt(mode: str, language: str, channel: str) -> str:
    """Build the system prompt used as DeepAgent identity/system baseline.

    Contains only the identity section. Other sections are injected by rails so
    they can still participate in global priority ordering at runtime.
    """
    if language == "zh":
        language = "cn"

    resolved_language = resolve_language(language)
    builder = SystemPromptBuilder(language=resolved_language)

    builder.add_section(_identity_prompt(resolved_language))

    return builder.build()


def _subagent_principle_prompt(language: str) -> str:
    """Execution principles for subagent."""
    if language == "cn" or language == "zh":
        return """# 执行任务的原则

- **直接进入正题** 少说「好的」「很乐意」，直接开始做事。
- **先想再做** 接到任务后先理清思路，想清楚步骤再执行，避免盲目行动。
- **办事严谨** 做事牢靠是获得信任的关键，不要让用户后悔使用你。
- **尽量不拒绝** 尽量满足用户的合理请求。只有在涉及违法、有害、侵犯他人权益或超出你能力范围时，才明确说明原因并拒绝。
- **隐私守护** 永远守护用户的隐私，除非用户明确要求分享。

## 输出规范
 - **避免重复** 不要重复表达相同的意思，每个想法只说一次。
 - **简洁明了** 不要用不同的措辞反复说同一件事。
 - **思考过程** 思考时直接说出计划，不要反复确认或重新表述。
 - **等待状态** 在等待任务完成时，只说一次"等待中"，不要重复。
"""
    else:
        return """# Execution Principles

- **Get straight to the point** Skip "Sure", "Happy to help"—just start doing the work.
- **Think before acting** After receiving a task, clarify your approach and steps before executing; avoid acting blindly.
- **Be reliable** Doing things well is the key to trust; don't make your user regret using you.
- **Try not to refuse** Fulfill reasonable requests whenever possible. Only refuse when something is illegal, harmful, infringes others' rights, or is beyond your capability—and explain why clearly.
- **Guard privacy** Always protect your user's privacy unless they explicitly ask to share.

## Output Guidelines
 - **Avoid repetition** Do not express the same idea multiple times; state each thought only once.
 - **Be concise and clear** Do not restate the same thing in different words.
 - **Thinking process** When thinking, directly state the plan; do not repeatedly confirm or rephrase.
 - **Waiting state** While waiting for a task to complete, say "waiting" only once; do not repeat.
"""


def _subagent_workspace_prompt(language: str, workspace_dir: Path | None = None) -> str:
    """Workspace prompt for subagent."""
    ws = workspace_dir or Path(get_agent_root_dir())
    if language == "cn" or language == "zh":
        return f"""## 工作区

你当前的工作路径为：{ws}
你可以自由在这个路径里操作文件，他们都属于你。如果用户没有要求在其他路径操作，默认将文件保存在此目录下。
"""
    else:
        return f"""## Workspace

You are working under the dir: {ws}.
Write or save all files under this dir, unless user ask you to operate in other dirs.
"""


def _subagent_time_prompt(language: str) -> str:
    """Current time prompt for subagent."""
    beijing_tz = timezone(timedelta(hours=8))
    now_str = datetime.now(tz=beijing_tz).strftime('%Y-%m-%d %H:%M:%S')
    if language == "cn" or language == "zh":
        return f"""# 当前时间

{now_str}
"""
    else:
        return f"""# Current Time

{now_str}
"""


def build_subagent_base_prompt(
    language: str = "zh",
    workspace_dir: Path | str | None = None,
    include_time: bool = True,
) -> str:
    """Build minimal system prompt for subagent (inherits parent safety rules).

    This is a lightweight version that only includes:
    - Basic identity definition
    - Safety rules (mandatory for security)
    - Response format (for correct message parsing)
    - Optional: workspace path and current time

    Excludes non-essential parts: skills, tools table, memory system, todo, etc.
    Estimated token reduction: ~70% (from ~3200 to ~800-1000 tokens)

    Args:
        language: language for the prompt ("zh" or "en")
        workspace_dir: workspace directory path
        include_time: whether to inject current time

    Returns:
        Minimal system prompt string for subagent
    """
    ws = Path(workspace_dir) if isinstance(workspace_dir, str) else workspace_dir

    # Normalize language
    if language == "zh":
        language = "cn"

    # Basic identity (simplified)
    if language == "cn":
        identity = """# 身份

你是一个 AI 助手的子代理（Subagent），专门执行父代理分派的特定任务。
你的职责是高效完成分配的任务，并将结果返回给父代理。

"""
    else:
        identity = """# Identity

You are a subagent of an AI assistant, specifically executing tasks assigned by the parent agent.
Your responsibility is to efficiently complete assigned tasks and return results to the parent agent.

"""

    parts = [identity]

    # Optional: current time
    if include_time:
        parts.append(_subagent_time_prompt(language) + '\n')

    # Optional: workspace
    parts.append(_subagent_workspace_prompt(language, workspace_dir=ws) + '\n')

    # Same OS / shell hints as main agent identity (fork & spawn use this builder)
    parts.append("---\n\n")
    parts.append(_runtime_environment_prompt(language) + '\n')

    parts.append(_subagent_principle_prompt(language) + '\n')

    # Safety rules are injected by SecurityRail (added by factory.py or explicitly in rails).
    # No need to hardcode here to avoid duplication with SDK's SAFETY_PROMPT_CN.

    return "".join(parts)


def _read_file(file_path: str) -> Optional[str]:
    """Read file content from workspace."""
    if not file_path:
        return None
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if content:
                return content
            return None
    except FileNotFoundError:
        logger.debug(f"File not found: {file_path}")
        return None
    except Exception as e:
        logger.error(f"Error reading {file_path}: {e}")
        return None
