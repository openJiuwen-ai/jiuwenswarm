# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Gateway 受控通道 slash 指令：单一解析与注册表（无 IO）.

与架构说明 docs/zh/SLASH_COMMAND_ARCHITECTURE.md 一致：此处仅 A 类通道控制与元数据登记，
客户端专有命令（如 /resume）仅记录在 FIRST_BATCH_REGISTRY 中，不在 Gateway 内执行。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal

# ---------------------------------------------------------------------------
# 合法控制消息全集（用于 IM 入站管线跳过 LLM 改写等，须与 Gateway 拦截语义一致）
# ---------------------------------------------------------------------------


class GatewaySlashCommand(str, Enum):
    """Gateway 当前支持解析的受控通道 slash 指令（A 类）。"""

    NEW_SESSION = "/new_session"
    MODE = "/mode"
    SWITCH = "/switch"
    SKILLS = "/skills"
    SKILLS_LIST = "/skills list"
    LS = "/ls"
    VIEW = "/view"


class ModeSubcommand(str, Enum):
    """`/mode` 支持的子命令。"""

    AGENT = "agent"
    CODE = "code"
    TEAM = "team"
    AGENT_PLAN = "agent.plan"
    AGENT_FAST = "agent.fast"
    CODE_PLAN = "code.plan"
    CODE_NORMAL = "code.normal"


_VALID_MODE_LINES: frozenset[str] = frozenset(
    f"{GatewaySlashCommand.MODE.value} {sub.value}" for sub in ModeSubcommand
)


class SwitchSubcommand(str, Enum):
    """`/switch` 支持的子命令。"""

    PLAN = "plan"
    FAST = "fast"
    NORMAL = "normal"


_VALID_SWITCH_LINES: frozenset[str] = frozenset(
    f"{GatewaySlashCommand.SWITCH.value} {sub.value}" for sub in SwitchSubcommand
)

CONTROL_MESSAGE_TEXTS: frozenset[str] = frozenset(
    {
        GatewaySlashCommand.NEW_SESSION.value,
        *_VALID_MODE_LINES,
        *_VALID_SWITCH_LINES,
        GatewaySlashCommand.SKILLS_LIST.value,
    }
)


class ParsedControlAction(str, Enum):
    """parse_channel_control_text 的判定结果。"""

    NONE = "none"
    NEW_SESSION_OK = "new_session_ok"
    NEW_SESSION_BAD = "new_session_bad"
    MODE_OK = "mode_ok"
    MODE_BAD = "mode_bad"
    SWITCH_OK = "switch_ok"
    SWITCH_BAD = "switch_bad"
    SKILLS_OK = "skills_ok"
    LS_OK = "ls_ok"
    VIEW_OK = "view_ok"


@dataclass(frozen=True)
class ParsedChannelControl:
    """受控通道用户整行文本解析结果（与 message_handler 原语义一致）。"""

    action: ParsedControlAction
    mode_subcommand: str | None = None
    """mode_ok 时为 agent|code|team|agent.plan|agent.fast|code.plan|code.normal 之一。"""
    switch_subcommand: str | None = None
    """switch_ok 时为 plan|fast|normal 之一。"""


def parse_channel_control_text(text: str) -> ParsedChannelControl:
    """解析单条用户文本是否为 /new_session、/mode、/switch、/skills list、/ls、/view 控制指令。

    - 含换行则视为非控制（与原 _handle_channel_control 一致）。
    - /new_session 仅整行精确匹配为合法；带后缀为非法但仍为控制指令。
    - /mode 仅白名单整行合法；支持 agent|code|team 及四个直达模式值；其它以 /mode 开头且单行非法。
    - /switch 仅白名单整行合法；其它以 /switch 开头且单行非法。
    - /skills list 仅整行精确匹配（/skills 本身不再触发）。
    - /ls 以 /ls 开头的单行文本。
    - /view 以 /view 开头的单行文本。
    """
    if not text:
        return ParsedChannelControl(ParsedControlAction.NONE)
    if "\n" in text:
        return ParsedChannelControl(ParsedControlAction.NONE)
    t = text.strip()
    normalized = " ".join(t.split())
    if t == GatewaySlashCommand.NEW_SESSION.value:
        return ParsedChannelControl(ParsedControlAction.NEW_SESSION_OK)
    if t.startswith(GatewaySlashCommand.NEW_SESSION.value):
        return ParsedChannelControl(ParsedControlAction.NEW_SESSION_BAD)
    if normalized == GatewaySlashCommand.SKILLS_LIST.value:
        return ParsedChannelControl(ParsedControlAction.SKILLS_OK)
    if t in _VALID_MODE_LINES:
        parts = t.split()
        sub = parts[1] if len(parts) >= 2 else ""
        return ParsedChannelControl(ParsedControlAction.MODE_OK, mode_subcommand=sub)
    if t in _VALID_SWITCH_LINES:
        parts = t.split()
        sub = parts[1] if len(parts) >= 2 else ""
        return ParsedChannelControl(ParsedControlAction.SWITCH_OK, switch_subcommand=sub)
    if t.startswith(GatewaySlashCommand.LS.value):
        return ParsedChannelControl(ParsedControlAction.LS_OK)
    if t.startswith(GatewaySlashCommand.VIEW.value):
        return ParsedChannelControl(ParsedControlAction.VIEW_OK)
    if t.startswith(GatewaySlashCommand.MODE.value):
        return ParsedChannelControl(ParsedControlAction.MODE_BAD)
    if t.startswith(GatewaySlashCommand.SWITCH.value):
        return ParsedChannelControl(ParsedControlAction.SWITCH_BAD)
    return ParsedChannelControl(ParsedControlAction.NONE)


def is_control_like_for_im_batching(text: str) -> bool:
    """飞书/企微等：控制类消息不走合并窗口（与历史行为一致并补全 mode 变体与 /skills list）。

    单条文本、且为已知控制句、或以 /mode / /switch / /new_session 为前缀（含非法变体）时返回 True。
    """
    if not text:
        return False
    if "\n" in text:
        return False
    t = text.strip()
    normalized = " ".join(t.split())
    if t in CONTROL_MESSAGE_TEXTS:
        return True
    if normalized == GatewaySlashCommand.SKILLS_LIST.value:
        return True
    if t.startswith(f"{GatewaySlashCommand.MODE.value} "):
        return True
    if t.startswith(f"{GatewaySlashCommand.SWITCH.value} "):
        return True
    if t.startswith(GatewaySlashCommand.SWITCH.value):
        return True
    if t.startswith(GatewaySlashCommand.NEW_SESSION.value):
        return True
    return False


# ---------------------------------------------------------------------------
# 第一批命令注册表（元数据；resume 等为 client scope）
# ---------------------------------------------------------------------------

SlashScope = Literal["gateway", "client"]


@dataclass(frozen=True)
class SlashCommandEntry:
    id: str
    canonical_text: str
    scope: SlashScope
    req_method: str | None
    notes: str


FIRST_BATCH_REGISTRY: tuple[SlashCommandEntry, ...] = (
    SlashCommandEntry(
        id="new_session",
        canonical_text=GatewaySlashCommand.NEW_SESSION.value,
        scope="gateway",
        req_method=None,
        notes="受控通道重置 session_id；由 MessageHandler 拦截，不转发 Agent 对话。",
    ),
    SlashCommandEntry(
        id="mode",
        canonical_text=f"{GatewaySlashCommand.MODE.value} agent|code|team|agent.plan|agent.fast|code.plan|code.normal",
        scope="gateway",
        req_method=None,
        notes="受控通道切换模式：一级模式 agent/code/team（映射到默认子模式）或直达 agent.plan/agent.fast/code.plan/code.normal；写入 params.mode。",
    ),
    SlashCommandEntry(
        id="switch",
        canonical_text=f"{GatewaySlashCommand.SWITCH.value} plan|fast|normal",
        scope="gateway",
        req_method=None,
        notes="受控通道切换二级模式：agent 下 plan/fast，code 下 plan/normal。",
    ),
    SlashCommandEntry(
        id="skills",
        canonical_text=GatewaySlashCommand.SKILLS_LIST.value,
        scope="gateway",
        req_method="skills.list",
        notes="受控通道整行 /skills list 时 Gateway 调 skills.list 并以通知回复；CLI 同路径见 builtins/skills.ts。",
    ),
    SlashCommandEntry(
        id="resume",
        canonical_text="/resume",
        scope="client",
        req_method="command.resume",
        notes="CLI 会话恢复；另用 session.list。IM 受控通道本阶段不解析，后续可扩展。",
    ),
    SlashCommandEntry(
        id="workspace_dir",
        canonical_text="/workspace_dir [get|set <path>|clear]",
        scope="client",
        req_method=None,
        notes="TUI 本地保存工作区路径；随 chat.send params.workspace_dir 发往 Gateway/AgentServer。",
    ),
)


def format_skills_list_for_notice(payload: dict[str, Any] | None, *, max_items: int = 50) -> str:
    """将 skills.list 响应 payload 格式化为适合 IM 的纯文本。"""
    if not payload or not isinstance(payload, dict):
        return "暂无技能数据。"
    err = payload.get("error")
    if isinstance(err, str) and err.strip():
        return f"获取技能列表失败：{err.strip()}"
    skills = payload.get("skills")
    if not isinstance(skills, list) or not skills:
        return "当前无可用技能。"
    lines: list[str] = ["【技能列表】"]
    for i, item in enumerate(skills[:max_items], 1):
        if isinstance(item, dict):
            name = str(item.get("name") or item.get("title") or "?").strip()
            desc = str(item.get("description") or "").strip()
            src = str(item.get("source") or "").strip()
            suffix = f" ({src})" if src else ""
            if desc:
                short = desc if len(desc) <= 200 else desc[:200] + "…"
                lines.append(f"{i}. {name}{suffix}\n   {short}")
            else:
                lines.append(f"{i}. {name}{suffix}")
        else:
            lines.append(f"{i}. {item}")
    if len(skills) > max_items:
        lines.append(f"... 共 {len(skills)} 项，仅显示前 {max_items} 项。")
    return "\n".join(lines)


# 供单测校验与外部只读引用（与 _VALID_MODE_LINES 相同）
VALID_MODE_LINES: frozenset[str] = _VALID_MODE_LINES
VALID_MODE_SUBCOMMANDS: tuple[str, ...] = tuple(sub.value for sub in ModeSubcommand)
VALID_SWITCH_LINES: frozenset[str] = _VALID_SWITCH_LINES
VALID_SWITCH_SUBCOMMANDS: tuple[str, ...] = tuple(sub.value for sub in SwitchSubcommand)
