# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Gateway 受控通道 slash 指令：单一解析与注册表（无 IO）.

与架构说明 docs/zh/SLASH_COMMAND_ARCHITECTURE.md 一致：此处仅 A 类通道控制与元数据登记，
客户端专有命令（如 /resume）仅记录在 FIRST_BATCH_REGISTRY 中，不在 Gateway 内执行。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
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
    BRANCH = "/branch"
    REWIND = "/rewind"
    REVIEW = "/review"
    SECURITY_REVIEW = "/security-review"
    JOIN = "/join"
    EXIT = "/exit"


class ModeSubcommand(str, Enum):
    """`/mode` 支持的子命令。"""

    AGENT = "agent"
    CODE = "code"
    TEAM = "team"
    AGENT_PLAN = "agent.plan"
    AGENT_FAST = "agent.fast"
    CODE_PLAN = "code.plan"
    CODE_NORMAL = "code.normal"
    CODE_TEAM = "code.team"


_VALID_MODE_LINES: frozenset[str] = frozenset(
    f"{GatewaySlashCommand.MODE.value} {sub.value}" for sub in ModeSubcommand
)


class SwitchSubcommand(str, Enum):
    """`/switch` 支持的子命令。"""

    PLAN = "plan"
    FAST = "fast"
    NORMAL = "normal"
    TEAM = "team"


_VALID_SWITCH_LINES: frozenset[str] = frozenset(
    f"{GatewaySlashCommand.SWITCH.value} {sub.value}" for sub in SwitchSubcommand
)

CONTROL_MESSAGE_TEXTS: frozenset[str] = frozenset(
    {
        GatewaySlashCommand.NEW_SESSION.value,
        *_VALID_MODE_LINES,
        *_VALID_SWITCH_LINES,
        GatewaySlashCommand.SKILLS_LIST.value,
        GatewaySlashCommand.BRANCH.value,
        GatewaySlashCommand.REWIND.value,
        GatewaySlashCommand.JOIN.value,
        GatewaySlashCommand.EXIT.value,
    }
)

# /join、/exit 的 session_ref 格式：team_<name>_session_<id>
_SESSION_REF_RE: re.Pattern[str] = re.compile(r'^team_[\w-]+_session_[\w-]+$')


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
    BRANCH_OK = "branch_ok"
    REWIND_OK = "rewind_ok"
    REWIND_BAD = "rewind_bad"
    REWIND_CONFIRM = "rewind_confirm"
    REWIND_CANCEL = "rewind_cancel"
    REVIEW_OK = "review_ok"
    REVIEW_BAD = "review_bad"
    SECURITY_REVIEW_OK = "security_review_ok"
    SECURITY_REVIEW_BAD = "security_review_bad"
    JOIN_OK = "join_ok"
    JOIN_BAD = "join_bad"
    EXIT_OK = "exit_ok"
    EXIT_BAD = "exit_bad"


@dataclass(frozen=True)
class ParsedChannelControl:
    """受控通道用户整行文本解析结果（与 message_handler 原语义一致）。"""

    action: ParsedControlAction
    mode_subcommand: str | None = None
    """mode_ok 时为 agent|code|team|agent.plan|agent.fast|code.plan|code.normal 之一。"""
    switch_subcommand: str | None = None
    """switch_ok 时为 plan|fast|normal 之一。"""
    branch_name: str | None = None
    """branch_ok 时为用户指定的分支名称（可为空字符串）。"""
    rewind_turn: int | None = None
    """rewind_ok 时为用户指定的回退轮次编号；None 表示未指定。"""
    rewind_pending_turn: int | None = None
    """rewind_ok 时记录原始轮次编号，用于 confirm/cancel 两步确认。"""
    pr_arg: str | None = None
    """review_ok 时为用户指定的 PR 编号、URL 或自由文本；空字符串表示未指定，将展示 PR 列表。"""
    security_review_arg: str | None = None
    """security_review_ok 时为用户可选附加说明；空字符串表示未指定。"""
    session_ref: str | None = None
    """join/exit 时的 session 引用。"""
    member_name: str | None = None
    """join 时的席位名。"""


_PR_ARG_MAX_LEN = 2048


def _sanitize_pr_arg(arg: str) -> str | None:
    """Pass-through /review args; reject only unsafe control chars or length."""
    if not arg:
        return ""
    if len(arg) > _PR_ARG_MAX_LEN:
        return None
    if any(ord(ch) < 32 for ch in arg):
        return None
    return arg


def parse_channel_control_text(text: str) -> ParsedChannelControl:
    """解析单条用户文本是否为 /new_session、/mode、/switch、/skills list、/branch、/rewind 控制指令。

    - 含换行则视为非控制（与原 _handle_channel_control 一致）。
    - /new_session 仅整行精确匹配为合法；带后缀为非法但仍为控制指令。
    - /mode 仅白名单整行合法；支持 agent|code|team 及四个直达模式值；其它以 /mode 开头且单行非法。
    - /switch 仅白名单整行合法；其它以 /switch 开头且单行非法。
    - /skills list 仅整行精确匹配（/skills 本身不再触发）。
    - /branch [name] 合法；name 为可选自定义分支标题。
    - /rewind [N] 合法；N 为可选回退轮次编号（正整数）；无参数或非整数参数为非法。
    - /rewind confirm N 确认执行之前发起的 /rewind N。
    - /rewind cancel 取消之前发起的 /rewind N。
    - /review [args] 合法；args 原样透传；无参数展示 PR 列表；
      过长或含控制字符为非法。
    - /security-review [args] 合法；args 原样透传；无参数审查当前分支变更；
      过长或含控制字符为非法。
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
    if t.startswith(GatewaySlashCommand.MODE.value):
        return ParsedChannelControl(ParsedControlAction.MODE_BAD)
    if t.startswith(GatewaySlashCommand.SWITCH.value):
        return ParsedChannelControl(ParsedControlAction.SWITCH_BAD)
    if t == GatewaySlashCommand.BRANCH.value:
        return ParsedChannelControl(ParsedControlAction.BRANCH_OK, branch_name="")
    if t.startswith(f"{GatewaySlashCommand.BRANCH.value} "):
        name = t[len(GatewaySlashCommand.BRANCH.value):].strip()
        return ParsedChannelControl(ParsedControlAction.BRANCH_OK, branch_name=name)
    if t == GatewaySlashCommand.REWIND.value:
        return ParsedChannelControl(ParsedControlAction.REWIND_BAD)
    # /rewind cancel — 取消之前的 /rewind（须在 /rewind N 前解析）
    if t == "/rewind cancel":
        return ParsedChannelControl(ParsedControlAction.REWIND_CANCEL)
    # /rewind confirm N — 二步确认执行（须在 /rewind N 前解析）
    if t.startswith("/rewind confirm "):
        arg = t[len("/rewind confirm "):].strip()
        try:
            turn = int(arg)
            if turn < 1:
                return ParsedChannelControl(ParsedControlAction.REWIND_BAD)
            return ParsedChannelControl(
                ParsedControlAction.REWIND_CONFIRM, rewind_turn=turn
            )
        except (ValueError, TypeError):
            return ParsedChannelControl(ParsedControlAction.REWIND_BAD)
    if t.startswith(f"{GatewaySlashCommand.REWIND.value} "):
        arg = t[len(GatewaySlashCommand.REWIND.value):].strip()
        try:
            turn = int(arg)
            if turn < 1:
                return ParsedChannelControl(ParsedControlAction.REWIND_BAD)
            return ParsedChannelControl(ParsedControlAction.REWIND_OK, rewind_turn=turn)
        except (ValueError, TypeError):
            return ParsedChannelControl(ParsedControlAction.REWIND_BAD)
    if t == GatewaySlashCommand.SECURITY_REVIEW.value:
        return ParsedChannelControl(
            ParsedControlAction.SECURITY_REVIEW_OK, security_review_arg=""
        )
    if t.startswith(f"{GatewaySlashCommand.SECURITY_REVIEW.value} "):
        arg = t[len(GatewaySlashCommand.SECURITY_REVIEW.value):].strip()
        if not arg:
            return ParsedChannelControl(
                ParsedControlAction.SECURITY_REVIEW_OK, security_review_arg=""
            )
        sanitized = _sanitize_pr_arg(arg)
        if sanitized is None:
            return ParsedChannelControl(ParsedControlAction.SECURITY_REVIEW_BAD)
        return ParsedChannelControl(
            ParsedControlAction.SECURITY_REVIEW_OK, security_review_arg=sanitized
        )
    if t == GatewaySlashCommand.REVIEW.value:
        return ParsedChannelControl(ParsedControlAction.REVIEW_OK, pr_arg="")
    if t.startswith(f"{GatewaySlashCommand.REVIEW.value} "):
        arg = t[len(GatewaySlashCommand.REVIEW.value):].strip()
        if not arg:
            return ParsedChannelControl(ParsedControlAction.REVIEW_OK, pr_arg="")
        sanitized = _sanitize_pr_arg(arg)
        if sanitized is None:
            return ParsedChannelControl(ParsedControlAction.REVIEW_BAD)
        return ParsedChannelControl(ParsedControlAction.REVIEW_OK, pr_arg=sanitized)
    # /join <session_ref> as <member_name>
    # 仅接受完整格式：/join team_<name>_session_<id> as <member_name>
    # 简化格式 /join <session_id> as <member_name> 不再允许：
    # 缺 team_name 维度无法做 team_name ↔ session_id 一致性校验，
    # 直接判格式错误，引导用户用完整格式。
    if t.startswith(GatewaySlashCommand.JOIN.value):
        parts = t.split()
        if (
            len(parts) == 4
            and parts[0] == GatewaySlashCommand.JOIN.value
            and parts[2] == "as"
        ):
            session_ref = parts[1]
            member_name = parts[3]
            if _SESSION_REF_RE.match(session_ref):
                return ParsedChannelControl(
                    ParsedControlAction.JOIN_OK,
                    session_ref=session_ref,
                    member_name=member_name,
                )
        return ParsedChannelControl(ParsedControlAction.JOIN_BAD)
    # /exit [session_ref]
    # 支持两种格式：
    #   不带参数: /exit（使用当前 session，不校验 team_name）
    #   完整: /exit team_<name>_session_<id>（handler 校验 team_name 与 session 一致）
    # 简化格式 /exit <session_id> 不再允许：缺 team_name 维度无法做一致性校验，
    # 直接判格式错误，引导用户用完整格式或无参 /exit。
    if t.startswith(GatewaySlashCommand.EXIT.value):
        parts = t.split()
        if len(parts) == 1 and parts[0] == GatewaySlashCommand.EXIT.value:
            # /exit 不带 session_id → handler 用当前 session 兜底，不做 team_name 校验
            return ParsedChannelControl(ParsedControlAction.EXIT_OK)
        if len(parts) == 2 and parts[0] == GatewaySlashCommand.EXIT.value:
            session_ref = parts[1]
            if _SESSION_REF_RE.match(session_ref):
                return ParsedChannelControl(
                    ParsedControlAction.EXIT_OK,
                    session_ref=session_ref,
                )
        return ParsedChannelControl(ParsedControlAction.EXIT_BAD)
    return ParsedChannelControl(ParsedControlAction.NONE)


def is_control_like_for_im_batching(text: str) -> bool:
    """飞书/企微等：控制类消息不走合并窗口（与历史行为一致并补全 mode 变体与 /skills list）。

    单条文本、且为已知控制句、或以 /mode / /switch / /new_session / /branch / /rewind 为前缀时返回 True。
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
    if t.startswith(GatewaySlashCommand.BRANCH.value):
        return True
    if t.startswith(GatewaySlashCommand.REWIND.value):
        return True
    if t.startswith(GatewaySlashCommand.REVIEW.value):
        return True
    if t.startswith(GatewaySlashCommand.SECURITY_REVIEW.value):
        return True
    if t.startswith(GatewaySlashCommand.JOIN.value):
        return True
    if t.startswith(GatewaySlashCommand.EXIT.value):
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
        canonical_text=f"{GatewaySlashCommand.MODE.value} agent|code|team|agent.plan|agent.fast|code.plan|"
                       f"code.normal|code.team|team.plan",
        scope="gateway",
        req_method=None,
        notes="受控通道切换模式：一级模式 agent/code/team（映射到默认子模式）；"
              "agent 的 plan/fast 已合并为单一 agent（agent.plan/agent.fast 作为历史别名仍可接受）；写入 params.mode。",
    ),
    SlashCommandEntry(
        id="switch",
        canonical_text=f"{GatewaySlashCommand.SWITCH.value} plan|fast|normal|team",
        scope="gateway",
        req_method=None,
        notes="受控通道切换二级模式：agent 下 plan/fast 已合并；code 下 plan/normal。",
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
    SlashCommandEntry(
        id="branch",
        canonical_text=f"{GatewaySlashCommand.BRANCH.value} [name]",
        scope="gateway",
        req_method="session.fork",
        notes="受控通道分叉当前会话；Gateway 调 session.fork 并以通知回复；CLI 同路径见 builtins/branch.ts。",
    ),
    SlashCommandEntry(
        id="rewind",
        canonical_text=f"{GatewaySlashCommand.REWIND.value} <turn_number>",
        scope="gateway",
        req_method="session.rewind",
        notes="受控通道回退对话到指定轮次；IM 须带正整数轮次编号；CLI 同路径见 builtins/rewind.ts。",
    ),
    SlashCommandEntry(
        id="recap",
        canonical_text="/recap",
        scope="client",
        req_method="command.recap",
        notes="客户端命令，生成会话快速回顾（read-only）；TUI → Gateway → AgentServer。",
    ),
    SlashCommandEntry(
        id="agents",
        canonical_text="/agents",
        scope="client",
        req_method="agents.list",
        notes="TUI agent 配置管理菜单；TUI 通过 agents.* 方法与后端交互。",
    ),
    SlashCommandEntry(
        id="review",
        canonical_text=f"{GatewaySlashCommand.REVIEW.value} [args]",
        scope="gateway",
        req_method=None,
        notes="受控通道代码审查：args 透传注入 prompt，"
              "由 Agent 执行 gh pr list/view/diff；无 git/gh 预检。",
    ),
    SlashCommandEntry(
        id="security-review",
        canonical_text=f"{GatewaySlashCommand.SECURITY_REVIEW.value} [args]",
        scope="gateway",
        req_method=None,
        notes="受控通道安全审查：args 透传注入 prompt，"
              "由 Agent 执行 git status/diff/log 并做安全分析；无 git 预检。",
    ),
    SlashCommandEntry(
        id="goal",
        canonical_text="/goal [set <objective>|pause|resume|clear]",
        scope="client",
        req_method="command.goal",
        notes="TUI session-level persistent goal management. "
              "SET/RESUME triggers a streaming goal round; "
              "GET/PAUSE/CLEAR returns status immediately.",
    ),
)


def _skill_source_tag(item: dict[str, Any]) -> str:
    """与 TUI ``listSkills`` 标签逻辑对齐：is_builtin_source→[builtin]，否则按 source 取 [local]/[project]/…。"""
    if item.get("is_builtin_source") is True or item.get("is_builtin") is True:
        return "[builtin]"
    src = str(item.get("source") or "").strip()
    if not src:
        return "[project]"
    if src == "local":
        return "[local]"
    return f"[{src}]"


def _truncate_desc_by_bytes(desc: str, max_bytes: int = 600) -> str:
    """按 UTF-8 字节预算截断描述，使中英文视觉长度一致。

    Python ``len()`` 数的是字符数：中文 1 字符 = 3 字节、英文 1 字符 = 1 字节，
    若按字符数截断会出现"中文描述很长很完整、英文描述一句话没说完就被切在单词
    中间（如 immediatel…）"的不一致。这里按字节预算截断：600 字节约等于 200 个
    汉字或 600 个英文字母，两边视觉长度相当，且都能讲清一句话。

    截断点优先落在最近的空格/换行（词界），避免把英文单词劈成两半；找不到词界
    时才在字节边界硬截。截断后补 ``…``。
    """
    if not desc:
        return ""
    encoded = desc.encode("utf-8")
    if len(encoded) <= max_bytes:
        return desc
    # 字节预算内能完整容纳的最大字符数：逐字符推进，直到加上下一个字符会超预算。
    cut_chars = 0
    used = 0
    for ch in desc:
        n = len(ch.encode("utf-8"))
        if used + n > max_bytes:
            break
        used += n
        cut_chars += 1
    prefix = desc[:cut_chars]
    # 词界兜底：把末尾不完整的英文单词去掉（回退到最后一个空格/换行）。
    if cut_chars < len(desc):
        last_sep = max(prefix.rfind(" "), prefix.rfind("\n"))
        if last_sep > 0:
            prefix = prefix[:last_sep]
    return prefix.rstrip() + "…"


def format_skills_list_for_notice(payload: dict[str, Any] | None, *, max_items: int = 50) -> str:
    """将 skills.list 响应 payload 格式化为适合 IM 的纯文本。

    与 TUI ``skills.ts`` 的 ``listSkills`` 渲染对齐：按 ``installed`` 字段分
    "已安装"/"可安装"两组，每项标注来源标签（[builtin]/[local]/[project]/…），
    使 IM 端 /skills list 与 TUI 显示一致。后端 ``handle_skills_list`` 对本地已装
    技能置 ``installed=True``、内置未装技能置 ``installed=False``，渲染据此分组。

    两组用醒目标题 + 空行分隔，且编号各自从 1 开始（不跨组连续），让用户一眼
    区分"已安装"与"可安装"。
    """
    if not payload or not isinstance(payload, dict):
        return "暂无技能数据。"
    err = payload.get("error")
    if isinstance(err, str) and err.strip():
        return f"获取技能列表失败：{err.strip()}"
    skills = payload.get("skills")
    if not isinstance(skills, list) or not skills:
        return "当前无可用技能。"

    installed: list[dict[str, Any]] = []
    available: list[dict[str, Any]] = []
    others: list[Any] = []  # 非 dict 项兜底，避免整条丢失
    for item in skills:
        if isinstance(item, dict):
            if item.get("installed") is True:
                installed.append(item)
            else:
                available.append(item)
        else:
            others.append(item)

    def _render_items(group: list[dict[str, Any]], quota: int) -> list[str]:
        # 编号每组独立从 1 开始，避免跨组连续让两组混作一坨。
        # quota 控制本组最多渲染多少项，使总输出受 max_items 约束。
        lines: list[str] = []
        for i, item in enumerate(group, 1):
            if i > quota:
                break
            name = str(item.get("name") or item.get("title") or "?").strip()
            tag = _skill_source_tag(item)
            desc = str(item.get("description") or "").strip()
            if desc:
                short = _truncate_desc_by_bytes(desc)
                lines.append(f"{i}. {name} {tag}\n   {short}")
            else:
                lines.append(f"{i}. {name} {tag}")
        return lines

    lines: list[str] = ["【技能列表】"]
    remaining = max_items  # 跨组共享的总配额：先满足已安装组，再给可安装组
    shown = 0

    if installed and remaining > 0:
        q = min(len(installed), remaining)
        lines.append("")
        lines.append(f"■ 已安装（{q}）")
        lines.extend(_render_items(installed, q))
        shown += q
        remaining -= q

    if available and remaining > 0:
        q = min(len(available), remaining)
        lines.append("")
        lines.append(f"■ 可安装（{q}）")
        lines.extend(_render_items(available, q))
        shown += q
        remaining -= q

    if others and remaining > 0:  # 兜底：非 dict 项独立编号，受剩余配额约束
        q = min(len(others), remaining)
        for i, item in enumerate(others[:q], 1):
            lines.append(f"{i}. {item}")
        shown += q
        remaining -= q

    if len(skills) > max_items and shown < len(skills):
        lines.append(f"... 共 {len(skills)} 项，仅显示前 {max_items} 项。")
    return "\n".join(lines)


# 供单测校验与外部只读引用（与 _VALID_MODE_LINES 相同）
VALID_MODE_LINES: frozenset[str] = _VALID_MODE_LINES
VALID_MODE_SUBCOMMANDS: tuple[str, ...] = tuple(sub.value for sub in ModeSubcommand)
VALID_SWITCH_LINES: frozenset[str] = _VALID_SWITCH_LINES
VALID_SWITCH_SUBCOMMANDS: tuple[str, ...] = tuple(sub.value for sub in SwitchSubcommand)
