# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""权限检查器 - 实现具体的权限检查逻辑

优先级规则:

  deny 绝对否决: 任何匹配到的 deny 规则都具有最高优先级。

  来源优先级 (用于 ask/allow 之间的决断):
    1. tools.<tool_name>.patterns[i]   工具级模式规则
    2. tools.<tool_name>.*             工具级默认
    3. defaults.*                       全局默认

  同一工具的 patterns 内部: deny > ask > allow (最严格者胜出)
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, List

from jiuwenclaw.agentserver.permissions.tiered_policy import (
    collect_builtin_permission_rail_tool_names,
)
from jiuwenclaw.agentserver.permissions.shell_tools import extract_shell_command

logger = logging.getLogger(__name__)


@dataclass
class ToolPermissionLog:
    """工具权限审批日志"""
    tool: str
    decision: str  # ALLOW / DENY / ASK / SKIP
    source: str    # system / user
    rule: str
    tag: str = "TOOL_PERMISSION"
    user_decision: str | None = None  # allow_always / allow_once / deny / timeout
    timestamp: str | None = None
    channel: str | None = None
    session_id: str | None = None

    def to_json(self) -> str:
        _ts = self.timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        data = {
            "timestamp": _ts,
            "tag": self.tag,
            "tool": self.tool,
            "decision": self.decision,
            "source": self.source,
            "rule": self.rule,
        }
        if self.user_decision:
            data["user_decision"] = self.user_decision
        data["channel"] = self.channel or "empty"
        data["session_id"] = self.session_id or "empty"
        return json.dumps(data, ensure_ascii=False)


# 当前 asyncio Task 的 channel_id（供工具权限判断）；由 interface 在 run_agent 前 set、结束后 reset。
# 不放入 Runner inputs，避免扩展对外契约。
TOOL_PERMISSION_CHANNEL_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "jiuwenclaw_tool_permission_channel_id",
    default="",
)

# 启用工具权限检查的通道集合。仅当通道 ID 在此集合内时才执行权限校验，其它通道跳过。
# web：浏览器；acp：Agent 协议（如 Cursor）；tui：本地 CLI。
PERMISSION_ENABLED_CHANNELS = frozenset({"web", "officeclaw", "acp", "tui"})


def collect_permission_rail_tool_names(permission_config: dict[str, Any]) -> list[str]:
    """构建 ``PermissionInterruptRail`` 应拦截的工具名列表（去重、排序）。
    合并 ``permissions.tools``、``permissions.rules[*].tools`` 以及内置
     ``builtin_rules.yaml``（与 ``config.yaml`` 同目录者优先）中的工具名，
     使内置参数规则仍走护栏。
    """
    names: set[str] = set()
    tools_cfg = permission_config.get("tools") or {}
    if isinstance(tools_cfg, dict):
        for key in tools_cfg.keys():
            label = str(key).strip()
            if label:
                names.add(label)
    rules = permission_config.get("rules") or []
    if isinstance(rules, list):
        for entry in rules:
            if not isinstance(entry, dict):
                continue
            raw_tools = entry.get("tools")
            if raw_tools is None:
                continue
            if isinstance(raw_tools, str):
                raw_tools = [raw_tools]
            if isinstance(raw_tools, list):
                for item in raw_tools:
                    if isinstance(item, str) and item.strip():
                        names.add(item.strip())
    for name in collect_builtin_permission_rail_tool_names():
        names.add(name)
    return sorted(names)


# ---------- 工具调用守卫 ----------


async def check_tool_permissions(
        tool_calls: List[Any],
        channel_id: str = "",
        session_id: str | None = None,
        session: Any = None,
        request_approval_callback: Callable[[Any, Any, Any], Awaitable[str]] | None = None,
) -> tuple[List[Any], List[tuple[Any, str]]]:
    """检查每个工具调用的权限，执行前过滤。

    Args:
        tool_calls: 待执行的工具调用列表
        channel_id: 频道 ID；仅通道在 PERMISSION_ENABLED_CHANNELS 内时执行校验，否则全量放行
        session_id: 会话 ID
        session: 会话对象，用于发起审批弹窗
        request_approval_callback: 当 needs_approval 时的回调 -> "allow_always"|"allow_once"|"deny"

    Returns:
        (allowed_tool_calls, denied_results)
        - allowed_tool_calls: 通过权限检查的工具调用
        - denied_results: [(tool_call, denial_message), ...] 被拒绝的调用
    """
    from jiuwenclaw.agentserver.permissions.core import get_permission_engine
    engine = get_permission_engine()
    if not engine.enabled:
        return list(tool_calls), []

    normalized_channel_id = (channel_id or "").strip()
    if normalized_channel_id not in PERMISSION_ENABLED_CHANNELS:
        logger.info(ToolPermissionLog(
            tool="N/A",
            decision="SKIP",
            source="system",
            rule="channel_filter",
            channel=normalized_channel_id,
            session_id=session_id,
        ).to_json())
        return list(tool_calls), []

    allowed: List[Any] = []
    denied: List[tuple[Any, str]] = []

    for tc in tool_calls:
        tool_name = getattr(tc, "name", "")
        tool_args = getattr(tc, "arguments", {})
        if isinstance(tool_args, str):
            try:
                tool_args = json.loads(tool_args)
            except Exception:
                tool_args = {}
        if not isinstance(tool_args, dict):
            tool_args = {}

        result = await engine.check_permission(
            tool_name=tool_name,
            tool_args=tool_args,
            channel_id=normalized_channel_id,
            session_id=session_id,
        )

        if result.is_allowed:
            allowed.append(tc)
            logger.info(ToolPermissionLog(
                tool=tool_name,
                decision="ALLOW",
                source="system",
                rule=result.matched_rule or "N/A",
                channel=normalized_channel_id,
                session_id=session_id,
            ).to_json())
        elif result.is_denied:
            deny_msg = f"[PERMISSION_DENIED] {result.reason or 'Operation not allowed'}"
            denied.append((tc, deny_msg))
            logger.warning(ToolPermissionLog(
                tool=tool_name,
                decision="DENY",
                source="system",
                rule=result.matched_rule or "N/A",
                channel=normalized_channel_id,
                session_id=session_id,
            ).to_json())
        elif result.needs_approval:
            logger.info(ToolPermissionLog(
                tool=tool_name,
                decision="ASK",
                source="system",
                rule=result.matched_rule or "N/A",
                channel=normalized_channel_id,
                session_id=session_id,
            ).to_json())
            if session is not None and request_approval_callback is not None:
                decision = await request_approval_callback(session, tc, result)
                if decision == "allow_always":
                    allowed.append(tc)
                    logger.info(ToolPermissionLog(
                        tool=tool_name,
                        decision="ALLOW",
                        source="user",
                        rule=result.matched_rule or "N/A",
                        user_decision=decision,
                        channel=normalized_channel_id,
                        session_id=session_id,
                    ).to_json())
                elif decision == "allow_once":
                    allowed.append(tc)
                    logger.info(ToolPermissionLog(
                        tool=tool_name,
                        decision="ALLOW",
                        source="user",
                        rule=result.matched_rule or "N/A",
                        user_decision=decision,
                        channel=normalized_channel_id,
                        session_id=session_id,
                    ).to_json())
                else:
                    denied.append(
                        (tc, "[PERMISSION_REJECTED] User rejected the request.")
                    )
                    logger.info(ToolPermissionLog(
                        tool=tool_name,
                        decision="DENY",
                        source="user",
                        rule=result.matched_rule or "N/A",
                        user_decision=decision,
                        channel=normalized_channel_id,
                        session_id=session_id,
                    ).to_json())
            else:
                denied.append(
                    (tc, f"[APPROVAL_REQUIRED] {result.reason}")
                )
                logger.info(ToolPermissionLog(
                    tool=tool_name,
                    decision="DENY",
                    source="user",
                    rule=result.matched_rule or "N/A",
                    user_decision="approval_unavailable",
                    channel=normalized_channel_id,
                    session_id=session_id,
                ).to_json())

    return allowed, denied


# ---------- 命令风险评估 ----------

_RISK_EVALUATION_PROMPT = (
    """你是一个安全审计专家。请评估以下工具调用的安全风险等级。
    1. 工具名称: {tool_name}。
    2. 调用参数: {tool_args}。
    3. 请严格按照以下 JSON 格式返回（不要输出其他内容）：
    '{{"level": "高|中|低", "explanation": "一句话先解释一下指令功能，再解释风险原因，面向普通用户"}}'
    4. 判断标准：
    - 高风险：从未知URL获取数据、向外部地址发送数据、请求用户凭证/令牌、访问MEMORY.md/USER.md等记忆数据、
             对任意数据进行base64解码、使用exec()/eval()执行外部数据、修改工作目录外的其它文件、
             执行混淆/编码的代码, 请求提权、读取浏览器的cookies/会话数据等、访问凭证相关的文件。
    - 中风险：读取workspace目录外的文件、操作浏览器、操作本地应用、执行代码/脚本。
    - 低风险：读取workspace目录下的文件、查看天气/格式化输出/计算器等不会改变系统状态的操作。
    """
)

_RISK_ICON_MAP = {"高": "\U0001f534", "中": "\U0001f7e1", "低": "\U0001f7e2"}
_RISK_LEVEL_RANK = {"低": 0, "中": 1, "高": 2}


def _normalize_risk_tool_args(tool_args: dict | str) -> dict:
    if isinstance(tool_args, str):
        try:
            parsed = json.loads(tool_args)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return tool_args if isinstance(tool_args, dict) else {}


def _risk_result(level: str, explanation: str) -> dict:
    return {
        "level": level,
        "explanation": explanation,
        "icon": _RISK_ICON_MAP.get(level, "\U0001f7e1"),
    }


def _select_highest_risk(reasons: list[tuple[str, str]]) -> dict | None:
    if not reasons:
        return None
    reasons.sort(key=lambda item: _RISK_LEVEL_RANK.get(item[0], 1), reverse=True)
    primary_level, primary_message = reasons[0]
    secondary_message = ""
    for level, message in reasons[1:]:
        if level == primary_level:
            continue
        secondary_message = message
        break
    explanation = primary_message
    if secondary_message:
        explanation = f"{primary_message}；同时{secondary_message}"
    return _risk_result(primary_level, explanation)


def _collect_static_command_risk_reasons(cmd: str) -> list[tuple[str, str]]:
    reasons: list[tuple[str, str]] = []
    if not cmd:
        return reasons

    stripped_cmd = cmd.strip()
    if stripped_cmd.startswith("$("):
        reasons.append(("高", "该命令使用命令替换作为实际执行命令，运行时执行目标不直观"))
    elif "$(" in stripped_cmd:
        reasons.append(("中", "该命令包含命令替换，实际参数会在运行时动态生成"))
    if re.match(r"^%[^%\s]+%", stripped_cmd):
        reasons.append(("中", "该命令使用环境变量展开作为实际执行命令，运行时执行目标不直观"))

    checks: list[tuple[str, str, str]] = [
        (
            "高",
            r"(?i)(^|[&|;(]\s*)(rm|del|erase)\b",
            "该命令包含删除文件操作，可能造成不可逆的数据丢失",
        ),
        (
            "高",
            r"(?i)(^|[&|;(]\s*)(rmdir|rd)\b[^\r\n&|;]*\s/[sq]\b",
            "该命令包含递归删除目录操作，可能删除目录及其所有内容",
        ),
        (
            "高",
            r"(?i)\b(remove-item|rm)\b[^\r\n&|;]*\s-(r|recurse|force|rf)\b",
            "该命令包含强制或递归删除操作，可能造成不可逆的数据丢失",
        ),
        (
            "高",
            r"(?i)\b(reg\s+delete|format|shutdown|reboot|mkfs|dd\s+if=|>\s*/dev/)\b",
            "该命令可能修改系统关键状态或造成系统损坏",
        ),
        (
            "高",
            r"(?i)\bpowershell(?:\.exe)?\b[^\r\n&|;]*(?:-enc|-encodedcommand)\b",
            "该命令包含编码后的 PowerShell 脚本，实际执行内容不易审查",
        ),
        (
            "高",
            r"(?i)\b(curl|wget|iwr|invoke-webrequest)\b[^\r\n|]*\|\s*(sh|bash|iex|powershell)\b",
            "该命令包含下载内容后直接执行的模式，存在执行不可信代码的风险",
        ),
        (
            "中",
            r"(?i)\b(sudo|chmod|chown|set-executionpolicy)\b",
            "该命令涉及权限或执行策略变更",
        ),
        (
            "中",
            r"(?i)\b(pip\s+install|npm\s+install|pnpm\s+install|yarn\s+add)\b",
            "该命令包含软件安装操作，会修改当前环境依赖",
        ),
        (
            "中",
            r"(?i)(^|[&|;(]\s*)(node|python|python3|py|ruby|perl|php|pwsh|powershell|cmd|bash|sh)\b",
            "该命令通过脚本解释器或 shell 执行入口运行代码，执行内容可能读取、写入文件或启动其他命令",
        ),
        (
            "中",
            r"(?i)(^|[&|;(]\s*)(npm|npx|pnpm|yarn)\b",
            "该命令通过包管理器或脚本运行器执行，可能运行项目脚本、安装依赖或修改当前环境",
        ),
        (
            "中",
            r"(?i)(^|[&|;(]\s*)(copy|xcopy|robocopy|move|ren|rename)\b",
            "该命令包含文件复制、移动或重命名操作，会修改文件状态",
        ),
        (
            "中",
            r"(?i)\b(new-item|set-content|add-content|out-file)\b",
            "该命令包含文件写入操作，会修改文件内容",
        ),
    ]
    for level, pattern, message in checks:
        if re.search(pattern, cmd):
            reasons.append((level, message))

    if re.search(r"(?<![<>=])>(?![>=])", cmd):
        reasons.append(("中", "该命令包含重定向写入 `>`，可能覆盖目标文件内容"))
    elif ">>" in cmd:
        reasons.append(("中", "该命令包含追加写入 `>>`，会修改目标文件内容"))
    return reasons


def assess_command_risk_static(tool_name: str, tool_args: dict | str) -> dict:
    """静态启发式风险评估（LLM 不可用时回退）.

    Returns:
        {"level": "高|中|低", "explanation": "...", "icon": "🔴|🟡|🟢"}
    """
    tool_args = _normalize_risk_tool_args(tool_args)
    cmd = extract_shell_command(tool_args)
    command_risk = _select_highest_risk(_collect_static_command_risk_reasons(cmd))
    if command_risk is not None:
        return command_risk
    if tool_name == "mcp_exec_command":
        return _risk_result("中", "该命令需要用户确认后执行")
    return _risk_result("低", "该操作风险较低")


def assess_shell_targets_risk_static(targets: list[str]) -> dict:
    """Evaluate risk for the permission targets shown in allow-always preview."""
    reasons: list[tuple[str, str]] = []
    for target in targets:
        text = str(target or "").strip()
        if not text:
            continue
        reasons.extend(_collect_static_command_risk_reasons(text))
    command_risk = _select_highest_risk(reasons)
    if command_risk is not None:
        return command_risk
    return _risk_result("低", "该命令持久化目标未命中明显危险操作")


async def assess_command_risk_with_llm(
        llm: Any,
        model_name: str,
        tool_name: str,
        tool_args: dict | str,
) -> dict:
    """使用 LLM 评估工具调用的安全风险.

    Args:
        llm: LLM 实例
        model_name: 模型名
        tool_name: 工具名
        tool_args: 工具参数

    Returns:
        {"level": "高|中|低", "explanation": "...", "icon": "🔴|🟡|🟢"}
        失败时回退到 assess_command_risk_static
    """
    if isinstance(tool_args, str):
        try:
            tool_args = json.loads(tool_args)
        except Exception:
            tool_args = {}

    args_str = ""
    try:
        args_str = json.dumps(tool_args, ensure_ascii=False)[:800]
    except Exception:
        args_str = str(tool_args)[:800]

    prompt = _RISK_EVALUATION_PROMPT.format(tool_name=tool_name, tool_args=args_str)

    try:
        from openjiuwen.core.foundation.llm import UserMessage
        ai_msg = await llm.invoke(
            model=model_name,
            messages=[UserMessage(content=prompt)],
        )
        raw = ai_msg.content if hasattr(ai_msg, "content") else str(ai_msg)

        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start >= 0 and end > start:
            parsed = json.loads(raw[start:end])
            level = parsed.get("level", "中")
            explanation = parsed.get("explanation", "")
            return {
                "level": level,
                "explanation": explanation,
                "icon": _RISK_ICON_MAP.get(level, "\U0001f7e1"),
            }
    except Exception:
        logger.debug("LLM risk assessment failed, using static fallback", exc_info=True)

    return assess_command_risk_static(tool_name, tool_args)


# Note: 旧 ExternalDirectoryChecker / _extract_paths_from_command / _looks_like_path
# 已在 Phase-1 删除，职责全部迁至 ``permissions/file_guard.py`` + ``permissions/files/extract.py``。
