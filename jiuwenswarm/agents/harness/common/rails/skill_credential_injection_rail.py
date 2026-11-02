# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Inject per-skill credentials into shell tool calls via tool_args["env"].

另含 SKILL_GATE 闸门：命令以「解释器 + 脚本」形态直接运行另一个有凭据技能
的脚本而该技能未激活时，拒绝执行并指引先调用 skill_tool 激活，防止凭据
静默缺失（脚本缺 key 失败）与跨技能凭据泄漏。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterable

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ToolCallInputs
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.common.rails.read_file_validation import (
    resolve_language_from_context,
)
from jiuwenswarm.agents.harness.common.rails.skill_active_state import (
    get_session_active_skill,
    resolve_skill_session_id,
)

logger = logging.getLogger(__name__)


def _nonempty_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


SHELL_PERMISSION_TOOLS = frozenset(
    {"bash", "shell", "mcp_exec_command", "create_terminal", "exec_command"}
)
_DEFAULT_SHELL_TYPES = frozenset({"auto", "cmd", "bash", "sh"})

SKILL_GATE_PREFIX = "[SKILL_GATE]"

# 单技能直跑指引：激活是单技能覆盖语义，激活后重跑即可满足（条件表述：
# 被拦的都是执行位脚本，但缺 key 与否取决于脚本是否实际读取凭据）。
GATE_MESSAGE_CN = (
    f"{SKILL_GATE_PREFIX} 检测到直接运行技能「{{skill}}」的脚本，但当前激活的技能不是它，"
    "其凭据环境变量不会被注入，脚本可能因缺少 key 而失败。"
    '请先调用 skill_tool(skill_name="{skill}")，然后重新执行原命令。'
)
GATE_MESSAGE_EN = (
    f"{SKILL_GATE_PREFIX} The command directly runs a script of skill \"{{skill}}\", "
    "which is not the active skill: its credential env vars will NOT be injected "
    'and the script may fail on a missing key. Call '
    'skill_tool(skill_name="{skill}") first, then rerun the original command.'
)

# 多技能复合命令指引（CR-1）：单激活语义下「激活 X 后重跑」会乒乓循环
# （激活 B 后 A 变 other 再被拦），该场景唯一可满足的指引是拆分命令。
GATE_MULTI_MESSAGE_CN = (
    f"{SKILL_GATE_PREFIX} 本条命令会同时执行多个技能（{{skills}}）的脚本，"
    "无法一次性注入全部凭据。请拆分为多条命令，逐个调用 "
    "skill_tool(skill_name=...) 激活对应技能后分别执行。"
)
GATE_MULTI_MESSAGE_EN = (
    f"{SKILL_GATE_PREFIX} This command executes scripts of multiple skills "
    "({skills}) at once; their credentials cannot all be injected in a single "
    "command. Split it into separate commands and activate each skill via "
    "skill_tool(skill_name=...) before running its script."
)

# ── 执行位判定（CR-1/CR-2）────────────────────────────────────────────
# 只匹配「将被执行」的脚本 token，不匹配参数位/只读引用：
#   · cat/echo/grep/sed/head/tail/diff/ls 等命令的参数位 .py 路径不命中
#   · --ref .../other/sample.py 等参数位引用不命中
#   · 命令替换 $(python3 x.py)、xargs 等罕见形态会漏拦（走回退注入，
#     安全面与旧行为持平，不产生误拦）
_INTERPRETER_BASENAMES = frozenset({"python", "python3", "py", "node"})
# 子命令前缀（wrapper）：跳过后继续找解释器/脚本（timeout 后可跟时长参数）
_COMMAND_PREFIX_BASENAMES = frozenset(
    {"nohup", "timeout", "time", "env", "nice", "stdbuf", "sudo", "ionice", "setsid"}
)
_SCRIPT_SUFFIXES = (".py", ".js", ".mjs")
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_TIMEOUT_DURATION_RE = re.compile(r"^[\d.]+[smh]?$")


def _split_subcommands(command: str) -> list[str]:
    """按引号外的 ``&& / || / ; / | / & / 换行`` 拆子命令。

    引号内的分隔符不拆（``sed 's|/path/x.py|X|'`` 的 ``|`` 是 s 命令
    分隔符）；``2>&1`` 等重定向的 ``&``（前字符为 ``>``）不拆。
    换行（含 ``\\r\\n`` 残留的 ``\\r``）与 ``;`` 同为顺序执行分隔符，
    多行复合命令（N1）必须逐行识别，否则第二行脚本绕过执行位判定。
    """
    parts: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        if quote is not None:
            buf.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ('"', "'"):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        # 二字符分隔符 && / || 必须先于单字符 ; / | / & 判定，
        # 否则 || 会被当成单个 | 拆开，产生空子命令； && 同理
        sep_len = 0
        if command[i:i + 2] in ("&&", "||"):
            sep_len = 2
        elif ch in ("\n", "\r", ";", "|", "&"):
            # 换行与 ; 同为顺序执行分隔符（多行复合命令，N1）；
            # 2>&1 等重定向的 & 不拆（前一个字符是 > ）
            if ch == "&" and i > 0 and command[i - 1] == ">":
                sep_len = 0
            else:
                sep_len = 1
        if sep_len:
            parts.append("".join(buf))
            buf = []
            i += sep_len
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return parts


def _tokenize(command: str) -> list[str]:
    """按空白拆 token；成对引号包裹的含空格内容保持单 token。"""
    tokens: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    for ch in command:
        if quote is not None:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ('"', "'"):
            quote = ch
            buf.append(ch)
            continue
        if ch.isspace():
            if buf:
                tokens.append("".join(buf))
                buf = []
            continue
        buf.append(ch)
    if buf:
        tokens.append("".join(buf))
    return tokens


def _strip_quotes(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ('"', "'"):
        return token[1:-1]
    return token


def _basename_lower(path: str) -> str:
    """取路径 basename（兼容正斜杠与反斜杠分隔、去 .exe 后缀、小写）。"""
    text = path.strip()
    if not text:
        return ""
    name = re.split(r"[\\/]+", text)[-1]
    if name.lower().endswith(".exe"):
        name = name[:-4]
    return name.lower()


def _is_interpreter_token(token: str) -> bool:
    return _basename_lower(_strip_quotes(token)) in _INTERPRETER_BASENAMES


def _is_script_token(token: str) -> bool:
    return _strip_quotes(token).lower().endswith(_SCRIPT_SUFFIXES)


def _iter_executed_scripts(subcommand: str) -> list[str]:
    """提取子命令中「将被执行」的脚本路径（执行位判定）。

    ① 首位是解释器（python/python3/py/node，含带引号绝对路径与 .exe
       形态）→ 跳过 flag 后的第一个脚本 token 为执行位（``python -c``
       的代码串不是路径 → 无执行位）；
    ② 首 token 自身是脚本（``./skill/run.py`` 直执行）→ 执行位；
    ③ 其他命令（cat/echo/grep/sed/ls…）→ 无执行位，参数位路径不扫。
    VAR=val 赋值前缀与 nohup/sudo/timeout 等 wrapper 前缀会被跳过。
    """
    tokens = _tokenize(subcommand)
    n = len(tokens)
    i = 0
    while i < n:
        raw = _strip_quotes(tokens[i])
        if _ENV_ASSIGN_RE.match(raw) and not _is_interpreter_token(raw):
            i += 1
            continue
        base = _basename_lower(raw)
        if base in _COMMAND_PREFIX_BASENAMES:
            i += 1
            if (
                base == "timeout"
                and i < n
                and _TIMEOUT_DURATION_RE.match(_strip_quotes(tokens[i]))
            ):
                i += 1
            continue
        break
    if i >= n:
        return []
    first = _strip_quotes(tokens[i])
    if _is_interpreter_token(first):
        j = i + 1
        while j < n and _strip_quotes(tokens[j]).startswith("-"):
            j += 1
        if j < n and _is_script_token(tokens[j]):
            return [_strip_quotes(tokens[j])]
        return []
    if _is_script_token(tokens[i]):
        return [first]
    return []


def match_skills_in_command(command: str, known_skills: Iterable[str]) -> list[str]:
    """Return every distinct known skill whose script the command *executes*.

    命令归一化（反斜杠→斜杠）后按 ``&& / || / ; / | / & / 换行`` 拆子命令
    （见 ``_split_subcommands``），对每个子命令做**执行位判定**（见
    ``_iter_executed_scripts``），再对执行位脚本路径**逐段**扫描，任一段
    精确命中已知技能名即算引用（整段边界，mx-a-x 不会误匹配 mx-a；
    大小写不敏感）。覆盖 ``<skill>/scripts/x.py`` 子目录与 ``<skill>/x.py``
    根目录布局、带引号绝对路径解释器的实录形态、wrapper 前缀、直执行
    与多行复合命令形态。按命令中出现顺序去重返回。

    注意不能用「/skill/ 后缀到脚本」的单个捕获组正则：finditer 非重叠
    匹配会让最靠前的无关段（如 /Object/、/relay-claw/）吞掉整个匹配。
    """
    if not command or not known_skills:
        return []
    canonical: dict[str, str] = {}
    for name in known_skills:
        text = _nonempty_str(name)
        if text:
            canonical.setdefault(text.lower(), text)
    if not canonical:
        return []
    matched: list[str] = []
    seen: set[str] = set()
    for subcommand in _split_subcommands(command.replace("\\", "/")):
        for script in _iter_executed_scripts(subcommand):
            for segment in re.split(r"/+", script):
                name = canonical.get(segment.strip().lower())
                if name is not None and name not in seen:
                    seen.add(name)
                    matched.append(name)
    return matched


def match_skill_in_command(command: str, known_skills: Iterable[str]) -> str | None:
    """Return the first known skill referenced by the command, else None."""
    matches = match_skills_in_command(command, known_skills)
    return matches[0] if matches else None


def build_skill_gate_message(skill: str, language: str = "cn") -> str:
    template = GATE_MESSAGE_EN if str(language).lower() == "en" else GATE_MESSAGE_CN
    return template.format(skill=skill)


def build_skill_gate_multi_message(skills: Iterable[str], language: str = "cn") -> str:
    template = (
        GATE_MULTI_MESSAGE_EN if str(language).lower() == "en" else GATE_MULTI_MESSAGE_CN
    )
    return template.format(skills=", ".join(skills))


def _write_gate_tool_result(ctx: AgentCallbackContext, message: str) -> None:
    """Skip 后给模型/UI 的错误结果（照抄 read_file_validation 的写入模式）。"""
    from openjiuwen.core.foundation.llm import ToolMessage

    if not isinstance(ctx.inputs, ToolCallInputs):
        return
    tool_call = ctx.inputs.tool_call
    tool_call_id = getattr(tool_call, "id", "") if tool_call else ""
    text = str(message or "")
    ctx.inputs.tool_result = text
    tool_msg = getattr(ctx.inputs, "tool_msg", None)
    if tool_msg is not None:
        tool_msg.content = text
        if tool_call_id and getattr(tool_msg, "tool_call_id", None) in (None, ""):
            tool_msg.tool_call_id = tool_call_id
    else:
        ctx.inputs.tool_msg = ToolMessage(content=text, tool_call_id=tool_call_id)


def _should_strip_powershell_amp_prefix(
    tool_name: str, command: Any, shell_type: Any
) -> bool:
    """True when bash command uses PowerShell '&' prefix under a non-PowerShell shell."""
    if tool_name != "bash":
        return False
    if not isinstance(command, str) or not command.lstrip().startswith("& "):
        return False
    return shell_type in _DEFAULT_SHELL_TYPES


def coalesce_skill_envs(
    incoming: dict[str, dict[str, str]] | None,
    current: dict[str, dict[str, str]] | None,
) -> dict[str, dict[str, str]]:
    """Prefer incoming skill envs; keep current when incoming is empty."""
    incoming_map = incoming if isinstance(incoming, dict) else {}
    current_map = current if isinstance(current, dict) else {}
    if incoming_map:
        return incoming_map
    return current_map or incoming_map


def coalesce_config_skill_envs(config: Any, previous: Any) -> Any:
    """Keep previous react.skill_envs when config only has the YAML placeholder."""
    if not isinstance(config, dict):
        return config
    react = config.get("react")
    incoming = react.get("skill_envs") if isinstance(react, dict) else None
    previous_envs = None
    if isinstance(previous, dict):
        prev_react = previous.get("react")
        if isinstance(prev_react, dict):
            previous_envs = prev_react.get("skill_envs")
    resolved = coalesce_skill_envs(
        incoming if isinstance(incoming, dict) else None,
        previous_envs if isinstance(previous_envs, dict) else None,
    )
    if resolved == incoming or (not resolved and not incoming):
        return config
    merged = dict(config)
    merged_react = dict(react) if isinstance(react, dict) else {}
    merged_react["skill_envs"] = resolved
    merged["react"] = merged_react
    return merged


class SkillCredentialInjectionRail(DeepAgentRail):
    """Inject per-skill credentials into shell tool calls.

    三分支（执行位判定，CR-1/CR-2 修正后）：
    1. 命令**执行**当前激活技能的脚本（执行位命中，matched == active）
       → 注入该技能凭据（配置权威覆盖：模型 env 已带的同名凭据 key 一律
       被当前配置值覆盖，防凭据轮换后模型从历史回写的旧值回流执行）；
       参数位/只读引用（cat/grep/--ref 等）不参与匹配；
    2. 命令**执行**另一个有凭据技能的脚本 → 闸门拒绝并指引激活（逃生口：
       tool_args["env"] 显式携带该技能凭据 key 时放行，放行前仅把模型已
       带入的同名凭据 key 刷新为当前配置值（陈旧值覆盖，不补全其余 key，
       跨技能隔离不变），且不再注入 active 技能的凭据）；执行位命中**多个**
       技能时指引拆分为多条命令分别激活（单激活语义下「激活后重跑」会
       乒乓循环）；
    3. 命令无执行位脚本（只读引用/cd+相对路径/裸文件名/无凭据技能/非
       脚本形态）→ 回退按 active 注入，与既有行为一致（零回归）。
    """

    priority = 5

    def __init__(
        self,
        skill_envs: dict[str, dict[str, str]] | None = None,
        preset_session_id: str | None = None,
    ) -> None:
        super().__init__()
        self._skill_envs: dict[str, dict[str, str]] = skill_envs or {}
        self._preset_session_id = _nonempty_str(preset_session_id)

    def update_skill_envs(self, new_skill_envs: dict[str, dict[str, str]]) -> None:
        self._skill_envs = new_skill_envs or {}
        logger.info(
            "[SkillCredentialInjectionRail] skill_envs updated: skills=[%s]",
            ", ".join(self._skill_envs.keys()),
        )

    def get_skill_envs(self) -> dict[str, dict[str, str]]:
        return self._skill_envs

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        if not isinstance(ctx.inputs, ToolCallInputs):
            return

        tool_name: str = ctx.inputs.tool_name
        if tool_name not in SHELL_PERMISSION_TOOLS:
            return

        session_id = self._resolve_session_id(ctx)
        # Read-only lookup: do not adopt default orphans here (cross-session
        # credential leak). Migration runs in SkillActiveStateRail.before_invoke.
        active_skill = get_session_active_skill(session_id)

        tool_args = self._coerce_tool_args_dict(ctx.inputs)
        command = (
            str(tool_args.get("command") or "") if isinstance(tool_args, dict) else ""
        )
        matched_skills = match_skills_in_command(command, self._skill_envs.keys())
        other_skills = [s for s in matched_skills if s != active_skill]

        if other_skills:
            if len(matched_skills) > 1:
                # CR-1：执行位命中多个技能（单激活语义下「激活 X 后重跑」
                # 会乒乓循环），指引拆分为多条命令分别激活执行。
                logger.info(
                    "[SkillCredentialInjectionRail] gate: blocked multi-skill "
                    "command skills=%s (active=%s) session=%s tool=%s",
                    matched_skills,
                    active_skill,
                    session_id,
                    tool_name,
                )
                self._reject_with_split_guidance(ctx, matched_skills)
                return
            gate_skill = other_skills[0]
            if self._has_explicit_skill_env(tool_args, gate_skill):
                # 逃生口：模型显式传了该技能的凭据 key，放行执行；
                # 不再注入 active 技能凭据（防跨技能泄漏）。
                # 配置权威覆盖：只刷新模型已带入的同名凭据 key（典型为凭据
                # 轮换后从历史回写的旧值抄写），不补全模型未提供的 key，
                # 跨技能隔离不变。
                gate_credentials = self._skill_envs.get(gate_skill, {})
                gate_env = tool_args.get("env") if isinstance(tool_args, dict) else None
                if gate_credentials and isinstance(gate_env, dict):
                    self._override_env_keys(
                        tool_args,
                        {k: v for k, v in gate_credentials.items() if k in gate_env},
                        tool_name,
                        skill=gate_skill,
                    )
                logger.info(
                    "[SkillCredentialInjectionRail] explicit env for skill=%s, "
                    "skip active-skill injection (active=%s) session=%s",
                    gate_skill,
                    active_skill,
                    session_id,
                )
                return
            logger.info(
                "[SkillCredentialInjectionRail] gate: blocked direct run of "
                "skill=%s (active=%s) session=%s tool=%s",
                gate_skill,
                active_skill,
                session_id,
                tool_name,
            )
            self._reject_with_guidance(ctx, gate_skill)
            return

        # matched 与 active 一致，或命令不可解析（cd+相对路径/裸文件名/
        # 无凭据技能/非脚本形态）：按 active 注入，与既有行为一致。
        if not active_skill:
            return

        credentials = self._skill_envs.get(active_skill, {})
        if not credentials:
            return

        # 注入凭据到 tool_args["env"]，BashTool 会转发给子进程。
        # 同时处理 PowerShell '&' 语法（去前缀保持 auto shell）。
        self._inject_credentials(
            ctx.inputs, credentials, tool_name, skill=active_skill
        )
        logger.debug(
            "[SkillCredentialInjectionRail] injected env keys=%s for skill=%s tool=%s",
            list(credentials.keys()),
            active_skill,
            tool_name,
        )

    def _coerce_tool_args_dict(
        self, inputs: ToolCallInputs
    ) -> dict[str, Any] | None:
        """JSON 字符串 tool_args 原地解析为 dict（与 _inject_credentials 共享）。"""
        tool_args = inputs.tool_args
        if isinstance(tool_args, dict):
            return tool_args
        if isinstance(tool_args, str):
            try:
                parsed = json.loads(tool_args)
            except (json.JSONDecodeError, TypeError):
                return None
            if isinstance(parsed, dict):
                inputs.tool_args = parsed
                return parsed
        return None

    def _has_explicit_skill_env(self, tool_args: Any, skill: str) -> bool:
        """逃生口判定：tool_args["env"] 显式携带了该技能的任一凭据 key。"""
        if not isinstance(tool_args, dict):
            return False
        env = tool_args.get("env")
        if not isinstance(env, dict):
            return False
        credential_keys = set(self._skill_envs.get(skill, {}))
        return bool(credential_keys & set(env))

    def _reject_with_guidance(self, ctx: AgentCallbackContext, skill: str) -> None:
        """拒绝执行（_skip_tool 短路）并写入激活指引，模型可据此自愈。"""
        language = resolve_language_from_context(ctx)
        message = build_skill_gate_message(skill, language)
        ctx.extra["_skip_tool"] = True
        _write_gate_tool_result(ctx, message)

    def _reject_with_split_guidance(
        self, ctx: AgentCallbackContext, skills: list[str]
    ) -> None:
        """多技能复合命令：指引拆分为多条命令分别激活执行（CR-1）。"""
        language = resolve_language_from_context(ctx)
        message = build_skill_gate_multi_message(skills, language)
        ctx.extra["_skip_tool"] = True
        _write_gate_tool_result(ctx, message)

    def _resolve_session_id(self, ctx: AgentCallbackContext) -> str:
        return resolve_skill_session_id(ctx, self._preset_session_id)

    def _override_env_keys(
        self,
        tool_args: dict[str, Any],
        credentials: dict[str, str],
        tool_name: str,
        skill: str | None = None,
    ) -> None:
        """配置权威覆盖：技能凭据 key 一律写入当前配置值。

        模型 env 里已存在的同名凭据 key 会被覆盖 —— 旧值典型来源是历史
        回写后被模型模仿抄写的陈旧凭据（凭据轮换后 tool_call 历史仍带旧
        值，模型照抄导致过期凭据透传执行）。仅覆盖技能凭据 key，模型传入
        的其他 env 项不动。
        """
        env = tool_args.get("env")
        if not isinstance(env, dict):
            env = {}
        for key, value in credentials.items():
            if value in (None, ""):
                continue
            if key in env and env.get(key) != value:
                logger.warning(
                    "[SkillCredentialInjectionRail] override env key=%s for "
                    "skill=%s tool=%s (previous value differs from current "
                    "skill_envs, e.g. stale credential copied from context)",
                    key,
                    skill or "-",
                    tool_name,
                )
            env[key] = value
        tool_args["env"] = env

    def _inject_credentials(
        self,
        inputs: ToolCallInputs,
        credentials: dict[str, str],
        tool_name: str,
        skill: str | None = None,
    ) -> None:
        tool_args: Any = inputs.tool_args
        if tool_args is None:
            return

        if isinstance(tool_args, str):
            try:
                parsed = json.loads(tool_args)
            except (json.JSONDecodeError, TypeError):
                return
            if not isinstance(parsed, dict):
                return
            tool_args = parsed
            inputs.tool_args = tool_args

        if isinstance(tool_args, dict):
            command = tool_args.get("command", "")

            # 当 bash 工具的命令以 PowerShell 调用操作符 '&' 开头时，
            # 去掉 '&' 前缀让命令在默认 shell(auto=Git Bash/cmd)下直接执行，
            # 避免 cmd 报 "& was unexpected" 及 Git Bash 的后台执行语义。
            # 若 LLM 显式指定了 powershell 则保留原样（尊重其意图）。
            shell_type = tool_args.get("shell_type", "auto")
            if _should_strip_powershell_amp_prefix(tool_name, command, shell_type):
                tool_args["command"] = command.lstrip()[2:].lstrip()

            self._override_env_keys(tool_args, credentials, tool_name, skill=skill)


__all__ = [
    "GATE_MESSAGE_CN",
    "GATE_MESSAGE_EN",
    "GATE_MULTI_MESSAGE_CN",
    "GATE_MULTI_MESSAGE_EN",
    "SHELL_PERMISSION_TOOLS",
    "SKILL_GATE_PREFIX",
    "SkillCredentialInjectionRail",
    "build_skill_gate_message",
    "build_skill_gate_multi_message",
    "coalesce_config_skill_envs",
    "coalesce_skill_envs",
    "match_skill_in_command",
    "match_skills_in_command",
]
