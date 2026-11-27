# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tiered tool permission policy.

The policy is intentionally small:

1. Whole-tool configuration wins first.
2. Shell parameter rules only match shell command text.
3. Deny rules scan the whole command before subcommand allow matching.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any

import yaml

from jiuwenclaw.agentserver.permissions.models import PermissionLevel
from jiuwenclaw.agentserver.permissions.patterns import match_wildcard
from jiuwenclaw.agentserver.permissions.shell_ast import parse_shell_for_permission
from jiuwenclaw.agentserver.permissions.shell_tools import (
    SHELL_PERMISSION_TOOLS,
    extract_shell_command,
    is_shell_permission_tool,
)

logger = logging.getLogger(__name__)

_STRICT_ORDER = {PermissionLevel.DENY: 0, PermissionLevel.ASK: 1, PermissionLevel.ALLOW: 2}

_MR = "tiered_policy"
_APPROVAL_OVERRIDES_PREFIX = f"{_MR}:approval_overrides"
_SHELL_SUBCOMMANDS_PREFIX = f"{_MR}:shell_subcommands"

_SHELL_TOOLS = SHELL_PERMISSION_TOOLS

# 仅 UI 追问、不向磁盘/网络执行敏感操作；未在 permissions.tools 中显式配置时，
# 若仍走 defaults.guard，则非 shell 未知工具会变成 ASK → PermissionRail 在工具体之前
# 弹出「权限审批」，导致 chat.ask_user_question 永远不发出，表现为「引导选择题不弹」。
_INTRINSIC_SAFE_UI_TOOL_NAMES: frozenset[str] = frozenset({
    "ask_user_question",
    "jiuwenclaw_ask_user_question",
})

# Backward-compatible exports for modules that still need path extraction for
# external_directory. They are no longer used by the tiered policy itself.
_PATH_TOOLS = frozenset({
    "read_file", "write_file", "edit_file",
    "read_text_file", "write_text_file",
    "write", "read", "Write", "Read", "Edit",
    "glob_file_search", "glob", "list_dir", "list_files",
    "grep", "search_replace",
})
_NETWORK_TOOLS = frozenset({"mcp_fetch_webpage", "mcp_free_search", "mcp_paid_search", "mcp_petal_search"})

_PATH_ARG_KEYS = frozenset({
    "path", "file_path", "target_file", "file", "old_path", "new_path",
    "source_path", "dest_path", "directory", "dir",
})

_BUILTIN_RULES_CACHE: tuple[str, float, list[dict[str, Any]]] | None = None

# Phase-1：``guard`` 表示"无 baseline"，让评估直接进入子线 A（命令/参数规则）。
# 历史 ``ask`` 配置会被静默升级为 ``guard``，并只打一次 deprecation 日志。
GUARD_LEVEL_LITERAL = "guard"
_LEGACY_ASK_DEPRECATED_TOOLS: set[str] = set()
_LEGACY_ASK_DEPRECATED_DEFAULTS: bool = False


def _package_builtin_rules_path() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "resources" / "builtin_rules.yaml"


def get_package_builtin_rules_path() -> Path:
    return _package_builtin_rules_path()


def _resolve_builtin_rules_yaml_path() -> Path | None:
    user_dir = os.getenv("JIUWENCLAW_CONFIG_DIR")
    if user_dir:
        user_path = Path(user_dir) / "builtin_rules.yaml"
        if user_path.is_file():
            return user_path
    fallback_user_path = Path.home() / ".jiuwenclaw" / "config" / "builtin_rules.yaml"
    if fallback_user_path.is_file():
        return fallback_user_path
    pkg_path = _package_builtin_rules_path()
    if pkg_path.is_file():
        return pkg_path
    logger.warning(
        "[PermissionEngine] permission.tiered_policy.builtin_rules_missing user_path=%s package_path=%s",
        fallback_user_path,
        pkg_path,
    )
    return None


def get_builtin_security_rules() -> list[dict[str, Any]]:
    global _BUILTIN_RULES_CACHE
    path = _resolve_builtin_rules_yaml_path()
    if path is None:
        return []
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = -1.0
    key = str(path.resolve())
    if _BUILTIN_RULES_CACHE is not None:
        ck, mt, rules = _BUILTIN_RULES_CACHE
        if ck == key and mt == mtime:
            return rules
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    rules = [r for r in (data.get("rules") or []) if isinstance(r, dict)]
    _BUILTIN_RULES_CACHE = (key, mtime, rules)
    return rules


def collect_builtin_permission_rail_tool_names() -> list[str]:
    """Return shell tools for callers that still display builtin coverage.

    PermissionRail no longer uses this as a security boundary.
    """
    return sorted(_SHELL_TOOLS)


def strictest(*levels: PermissionLevel) -> PermissionLevel:
    if not levels:
        return PermissionLevel.ASK
    return min(levels, key=lambda p: _STRICT_ORDER[p])


def _parse_level(value: str) -> PermissionLevel:
    return PermissionLevel((value or "").strip().lower())


def _is_shell_tool(tool_name: str) -> bool:
    return is_shell_permission_tool(tool_name)


def _command_text(tool_args: dict[str, Any]) -> str:
    return extract_shell_command(tool_args)


def _shell_pattern_matches(pattern: str, command: str, scope: str = "") -> bool:
    if not pattern or not command:
        return False
    p = pattern.strip()
    normalized_scope = str(scope or "").strip().lower()
    if normalized_scope == "exact":
        return command == p
    if p.lower().startswith("re:"):
        expr = p[3:].strip()
        flags = re.IGNORECASE if sys.platform == "win32" else 0
        norm = command.replace("\\", "/")
        try:
            return bool(re.search(expr, command, flags) or (norm != command and re.search(expr, norm, flags)))
        except re.error:
            logger.warning("[PermissionEngine] permission.tiered_policy.invalid_shell_regex expr=%r", expr)
            return False
    if command == p:
        return True
    return match_wildcard(command, p) if any(ch in p for ch in "*?[") else command == p


def _rule_scope(rule: dict[str, Any]) -> str:
    raw = str(rule.get("scope") or "").strip().lower()
    if raw in {"exact", "head", "regex", "wildcard"}:
        return raw
    pattern = str(rule.get("pattern") or "").strip()
    if pattern.lower().startswith("re:"):
        return "regex"
    if pattern.endswith(" *"):
        return "wildcard"
    return "exact"


def _command_head(command: str) -> str:
    text = (command or "").strip()
    if not text:
        return ""
    dynamic_head = _dynamic_command_head(text)
    if dynamic_head:
        return dynamic_head
    try:
        argv = shlex.split(text, posix=(os.name != "nt"))
    except ValueError:
        argv = text.split()
    return str(argv[0]).strip() if argv else ""


def _dynamic_command_head(text: str) -> str:
    if text.startswith("$("):
        depth = 0
        for idx, ch in enumerate(text):
            if ch == "$" and idx + 1 < len(text) and text[idx + 1] == "(":
                depth += 1
            elif ch == ")" and depth:
                depth -= 1
                if depth == 0:
                    return text[:idx + 1].strip()
        return ""
    if text.startswith("${"):
        end = text.find("}")
        return text[:end + 1].strip() if end > 2 else ""
    if text.startswith("$"):
        match = re.match(r"^\$[A-Za-z_][A-Za-z0-9_]*", text)
        return match.group(0) if match else ""
    if text.startswith("%"):
        match = re.match(r"^%[^%\s]+%", text)
        return match.group(0) if match else ""
    return ""


def _allow_rule_matches_invocation(rule: dict[str, Any], invocation: str) -> bool:
    pattern = str(rule["pattern"])
    scope = _rule_scope(rule)
    if scope == "head":
        head = _command_head(invocation)
        return bool(head and pattern.strip() == f"{head} *")
    if _shell_pattern_matches(pattern, invocation, scope=scope):
        return True
    head = _command_head(invocation)
    return bool(head and pattern.strip() == f"{head} *")


def _iter_shell_rules(rules: list[dict[str, Any]], action: str) -> list[dict[str, Any]]:
    expected = action.strip().lower()
    out: list[dict[str, Any]] = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if str(rule.get("action") or "").strip().lower() != expected:
            continue
        pattern = rule.get("pattern")
        if not isinstance(pattern, str) or not pattern.strip():
            continue
        out.append(rule)
    return out


def _rule_label(namespace: str, rule: dict[str, Any]) -> str:
    rid = rule.get("id", "")
    return f"{namespace}[{rid}]" if rid else f"{namespace}[?]"


def _scan_whole_command_deny(
    command: str,
    builtin_rules: list[dict[str, Any]],
    user_rules: list[dict[str, Any]],
) -> tuple[bool, str | None]:
    hits: list[str] = []
    for rule in _iter_shell_rules(builtin_rules, "deny"):
        if _shell_pattern_matches(str(rule["pattern"]), command, scope=_rule_scope(rule)):
            hits.append(_rule_label("builtin", rule))
    for rule in _iter_shell_rules(user_rules, "deny"):
        if _shell_pattern_matches(str(rule["pattern"]), command, scope=_rule_scope(rule)):
            hits.append(_rule_label("rules", rule))
    if hits:
        return True, f"{_MR}:whole_command_deny:" + "+".join(sorted(set(hits)))
    return False, None


def _evaluate_subcommand_allow(
    subcommand_text: str,
    user_rules: list[dict[str, Any]],
    overrides: list[dict[str, Any]],
    builtin_rules: list[dict[str, Any]],
) -> tuple[PermissionLevel, str]:
    for namespace, rules in (
        ("approval_overrides", overrides),
        ("rules", user_rules),
        ("builtin", builtin_rules),
    ):
        for rule in _iter_shell_rules(rules, "allow"):
            if _allow_rule_matches_invocation(rule, subcommand_text):
                return PermissionLevel.ALLOW, f"{_MR}:{namespace}:{_rule_label(namespace, rule)}"
    return PermissionLevel.ASK, f"{_MR}:fallback(no_allow_match)"


def _defaults_guard_baseline(tool_name: str) -> tuple[PermissionLevel | None, str | None]:
    """``defaults`` 落在 guard（含历史 ask 升级、非法兜底）时的档位。

    与 ``tools.<name>: guard`` 对称：**非 shell** 无子线 A 命令可评 → ``ASK``；
    **shell** → ``(None, …)`` 继续走子线 A。
    """
    if not _is_shell_tool(tool_name):
        return PermissionLevel.ASK, "defaults.guard"
    return None, "defaults.guard"


def _default_level(permission_config: dict[str, Any], tool_name: str) -> tuple[PermissionLevel | None, str | None]:
    """解析 ``permissions.defaults``（需 ``tool_name`` 以区分 shell / 非 shell 的 guard 语义）.

    Phase-1 规则：

    - ``guard`` / 历史 ``ask`` / 非法兜底 guard：**shell** → ``(None, "defaults.guard")``；
      **非 shell** → ``(ASK, "defaults.guard")``（与显式 ``tools.*: guard`` 一致）。
    - ``allow / deny`` → 原样返回
    - 非标量 → 兜底 guard（同上 shell / 非 shell 分流）
    """
    if tool_name in _INTRINSIC_SAFE_UI_TOOL_NAMES:
        return PermissionLevel.ALLOW, f"{_MR}:intrinsic_safe_ui:{tool_name}"

    raw = permission_config.get("defaults", GUARD_LEVEL_LITERAL)
    if isinstance(raw, str):
        norm = raw.strip().lower()
        if norm == GUARD_LEVEL_LITERAL:
            return _defaults_guard_baseline(tool_name)
        if norm == "ask":
            global _LEGACY_ASK_DEPRECATED_DEFAULTS
            if not _LEGACY_ASK_DEPRECATED_DEFAULTS:
                logger.info(
                    "[PermissionEngine] permission.tiered_policy.legacy_ask_default_upgraded value=%r "
                    "hint=请把 permissions.defaults 改为 'guard'",
                    raw,
                )
                _LEGACY_ASK_DEPRECATED_DEFAULTS = True
            return _defaults_guard_baseline(tool_name)
        try:
            level = _parse_level(raw)
            return level, f"defaults.{level.value}"
        except ValueError:
            logger.warning(
                "[PermissionEngine] permission.tiered_policy.invalid_default_level value=%r fallback=guard",
                raw,
            )
            return _defaults_guard_baseline(tool_name)
    logger.warning(
        "[PermissionEngine] permission.tiered_policy.invalid_default_level reason=non_scalar_level "
        "value=%r fallback=guard",
        raw,
    )
    return _defaults_guard_baseline(tool_name)


def _baseline_level(
    permission_config: dict[str, Any],
    tools_cfg: dict[str, Any],
    tool_name: str,
) -> tuple[PermissionLevel | None, str | None]:
    """解析单个工具的 baseline 档位。

    Phase-1 规则：

    - 显式写 ``guard``：**shell 工具**返回 ``(None, "tools.<name>")``，让子线 A（命令规则）
      继续评估；**非 shell 工具**子线 A 无命令串可评，直接返回 ``(ASK, "tools.<name>")``，
      避免误用 ``baseline is None`` 分支把档位显示成「子线 A 放行 ALLOW」。
    - 历史 ``ask``：等价于 ``guard``，只在首次出现时打 INFO，**不**打 WARN；shell / 非 shell
      分流与上条相同。
    - ``allow / deny``：原样返回。
    - 工具未配置：交由 ``_default_level``。
    - 非法值或非标量：返回 ``(None, None)``。
    """
    if tool_name not in tools_cfg:
        return _default_level(permission_config, tool_name)
    raw = tools_cfg[tool_name]
    if isinstance(raw, str):
        norm = raw.strip().lower()
        if norm == GUARD_LEVEL_LITERAL:
            if not _is_shell_tool(tool_name):
                return PermissionLevel.ASK, f"tools.{tool_name}"
            return None, f"tools.{tool_name}"
        if norm == "ask":
            if tool_name not in _LEGACY_ASK_DEPRECATED_TOOLS:
                logger.info(
                    "[PermissionEngine] permission.tiered_policy.legacy_ask_upgraded tool=%s value=%r "
                    "hint=请把 tools.%s 改为 'guard'",
                    tool_name,
                    raw,
                    tool_name,
                )
                _LEGACY_ASK_DEPRECATED_TOOLS.add(tool_name)
            if not _is_shell_tool(tool_name):
                return PermissionLevel.ASK, f"tools.{tool_name}"
            return None, f"tools.{tool_name}"
        try:
            return _parse_level(raw), f"tools.{tool_name}"
        except ValueError:
            logger.warning(
                "[PermissionEngine] permission.tiered_policy.invalid_tool_level tool=%s value=%r",
                tool_name,
                raw,
            )
            return None, None
    logger.warning(
        "[PermissionEngine] permission.tiered_policy.invalid_tool_baseline tool=%s reason=non_scalar_level",
        tool_name,
    )
    return None, None


def _subcommands_for_evaluation(command: str) -> tuple[str, ...]:
    shell_parse = parse_shell_for_permission(command)
    if shell_parse.kind == "simple" and shell_parse.subcommands:
        subcommands = tuple(item.text for item in shell_parse.subcommands if item.text)
        return _filter_dynamic_head_nested_invocations(subcommands)
    if shell_parse.all_invocations:
        return _filter_dynamic_head_nested_invocations(shell_parse.all_invocations)
    return ()


def _filter_dynamic_head_nested_invocations(invocations: tuple[str, ...]) -> tuple[str, ...]:
    dynamic_heads = [
        _dynamic_command_head(invocation.strip())
        for invocation in invocations
        if invocation.strip()
    ]
    dynamic_heads = [head for head in dynamic_heads if head.startswith("$(")]
    if not dynamic_heads:
        return invocations

    out: list[str] = []
    for invocation in invocations:
        text = invocation.strip()
        if not text:
            continue
        if any(text != head and text in head for head in dynamic_heads):
            continue
        out.append(invocation)
    return tuple(out)


def _aggregate_subcommand_results(
    results: list[tuple[str, PermissionLevel, str]],
) -> tuple[PermissionLevel, str]:
    if not results:
        return PermissionLevel.ASK, f"{_MR}:shell_subcommands:fallback"
    final = strictest(*(permission for _, permission, _ in results))
    contributing = sorted({
        f"{command}=>{matched_rule}"
        for command, permission, matched_rule in results
        if permission == final
    })
    return final, f"{_SHELL_SUBCOMMANDS_PREFIX}:" + "+".join(contributing)


def evaluate_tiered_policy(
    permission_config: dict[str, Any],
    tool_name: str,
    tool_args: dict[str, Any],
) -> tuple[PermissionLevel, str]:
    permission, matched_rule, _ = evaluate_tiered_policy_detailed(permission_config, tool_name, tool_args)
    return permission, matched_rule


def evaluate_tiered_policy_detailed(
    permission_config: dict[str, Any],
    tool_name: str,
    tool_args: dict[str, Any],
) -> tuple[PermissionLevel, str, list[tuple[str, PermissionLevel, str]] | None]:
    tools_cfg = permission_config.get("tools") or {}
    if not isinstance(tools_cfg, dict):
        tools_cfg = {}
    rules = permission_config.get("rules") or []
    if not isinstance(rules, list):
        rules = []
    approval_overrides = permission_config.get("approval_overrides") or []
    if not isinstance(approval_overrides, list):
        approval_overrides = []

    baseline, baseline_rule = _baseline_level(permission_config, tools_cfg, tool_name)
    if baseline == PermissionLevel.DENY:
        return PermissionLevel.DENY, baseline_rule or f"{_MR}:tools.deny", None
    if baseline == PermissionLevel.ALLOW:
        return PermissionLevel.ALLOW, baseline_rule or f"{_MR}:tools.allow", None

    if baseline is None:
        # 仅兜底：如非法 ``tools.<name>`` 档位得到 ``(None, None)``；默认 / 显式 guard 下的非 shell
        # 已在 ``_default_level`` / ``_baseline_level`` 提升为 ASK，不会落到此处。
        if not _is_shell_tool(tool_name):
            return PermissionLevel.ALLOW, baseline_rule or f"{_MR}:guard_no_rules", None
        # shell 工具继续走子线 A 的命令/参数规则评估。
    elif baseline == PermissionLevel.ASK:
        # 非 guard 的合法 ASK（理论上 Phase-1 不再产生），保持旧语义。
        if not _is_shell_tool(tool_name):
            return PermissionLevel.ASK, baseline_rule or f"{_MR}:non_shell_ask", None

    command = _command_text(tool_args)
    if not command:
        return PermissionLevel.ASK, f"{_MR}:shell_empty_command", None

    builtin_rules = get_builtin_security_rules()
    denied, deny_rule = _scan_whole_command_deny(command, builtin_rules, rules)
    if denied:
        return PermissionLevel.DENY, deny_rule or f"{_MR}:whole_command_deny", None

    invocations = _subcommands_for_evaluation(command)
    if not invocations:
        level, rule = _evaluate_subcommand_allow(command, rules, approval_overrides, builtin_rules)
        return level, rule, None

    subcommand_results: list[tuple[str, PermissionLevel, str]] = []
    for invocation in invocations:
        level, rule = _evaluate_subcommand_allow(invocation, rules, approval_overrides, builtin_rules)
        subcommand_results.append((invocation, level, rule))

    final_level, final_rule = _aggregate_subcommand_results(subcommand_results)
    return final_level, final_rule, subcommand_results


def matched_rule_uses_approval_override(matched_rule: str | None) -> bool:
    return isinstance(matched_rule, str) and matched_rule.startswith(_APPROVAL_OVERRIDES_PREFIX)


def _tool_arg_value_looks_like_path(arg_key: str, value: str) -> bool:
    if arg_key in _PATH_ARG_KEYS:
        return True
    if "/" in value or "\\" in value:
        return True
    return len(value) > 1 and value[1] == ":"


def _iter_path_strings(_tool_name: str, tool_args: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for k, v in tool_args.items():
        if not isinstance(v, str) or not v.strip():
            continue
        if _tool_arg_value_looks_like_path(k, v):
            out.append(v.strip())
    return out
