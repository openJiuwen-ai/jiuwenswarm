# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""模式匹配器 - 仅支持 wildcard 模式；含权限规则持久化.

wildcard 模式：
- * → .*  (零个或多个)
- ? → .   (恰好一个)
- 正则元字符转义
- " *" 结尾 → ( .*)? 便于 "ls *" 匹配 "ls" 或 "ls -la"
- 全串锚定 ^...$ 防注入
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from jiuwenclaw.agentserver.permissions.suggestions import (
    PermissionSuggestion,
    build_permission_suggestions,
)
from jiuwenclaw.agentserver.permissions.shell_tools import (
    SHELL_PERMISSION_TOOLS,
    extract_shell_command,
    is_shell_permission_tool,
)

logger = logging.getLogger(__name__)

_SHELL_APPROVAL_TOOLS = SHELL_PERMISSION_TOOLS


@dataclass(frozen=True)
class _ApprovalOverrideSignature:
    pattern: str
    existing_pattern: str | None
    existing_action: str


# 限制性字符类：仅允许命令参数和路径常见字符，排除 ; | & ` < > $ 等 shell 元字符防注入
# - 置于开头避免被解析为范围
_WILDCARD_CHARS = r'[-a-zA-Z0-9 \._/:"\']'


def match_wildcard(value: str, pattern: str) -> bool:
    """通配符匹配.

    - * → 限制性字符类* (排除 shell 元字符，防命令拼接)
    - ? → 限制性字符类 (恰好一个)
    - 正则元字符转义
    - " *" 结尾 → ( 字符类*)? 使 "ls *" 可匹配 "ls" 或 "ls -la"
    - 全串锚定 ^...$ 防止 "git status; rm -rf /" 匹配 "git status *"

    Args:
        value: 被匹配字符串（来自工具输入）
        pattern: 通配符模式（来自配置，可信）

    Returns:
        是否匹配
    """
    if not pattern or not value:
        return False
    val = value.replace("\\", "/")
    pat = pattern.replace("\\", "/")
    # 1. 转义正则特殊字符（* 和 ? 保留，后续单独处理）
    to_escape = set(".+^${}()|[]\\")
    escaped = "".join("\\" + c if c in to_escape else c for c in pat)
    # 2. 先替换 ?（必须在 * 之前，否则会误替换 ")? " 中的 ?）
    escaped = escaped.replace("?", _WILDCARD_CHARS)
    # 3. * → 限制性字符类*
    if escaped.endswith(" *"):
        escaped = escaped[:-2] + "( " + _WILDCARD_CHARS + "*)?"
    else:
        escaped = escaped.replace("*", _WILDCARD_CHARS + "*")
    # 3. 全串锚定
    flags = re.IGNORECASE if sys.platform == "win32" else 0
    try:
        return bool(re.match("^" + escaped + "$", val, flags))
    except re.error:
        return False




class PatternMatcher:
    """模式匹配器 - 仅支持 wildcard 模式 (*, ?)."""

    @staticmethod
    def match(pattern: str, value: str) -> bool:
        if not pattern or not value:
            return False
        return match_wildcard(value, pattern)

    def match_any(self, patterns: list[str], value: str) -> bool:
        """匹配任意一个模式."""
        return any(self.match(p, value) for p in patterns)


class PathMatcher:
    """路径匹配器."""

    def __init__(self):
        self._pm = PatternMatcher()

    def match_path(self, pattern: str, path: str | Path) -> bool:
        """匹配文件路径 (规范化分隔符后再比较)."""
        normalized_path = str(path).replace("\\", "/")
        normalized_pattern = pattern.replace("\\", "/")

        if self._pm.match(normalized_pattern, normalized_path):
            return True

        # 尝试匹配父目录层级
        path_obj = Path(str(path))
        for parent in path_obj.parents:
            parent_str = str(parent).replace("\\", "/")
            if self._pm.match(normalized_pattern, parent_str):
                return True
            if self._pm.match(normalized_pattern, parent_str + "/"):
                return True
            if self._pm.match(normalized_pattern, parent_str + "/*"):
                return True
        return False

    def match_path_any(self, patterns: list[str], path: str | Path) -> bool:
        return any(self.match_path(p, path) for p in patterns)


class URLMatcher:
    """URL 匹配器."""

    def __init__(self):
        self._pm = PatternMatcher()

    def match_url(self, pattern: str, url: str) -> bool:
        """匹配 URL (支持 hostname、netloc、full URL)."""
        if not url:
            return False
        if self._pm.match(pattern, url):
            return True
        try:
            parsed = urlparse(url)
            if self._pm.match(pattern, parsed.hostname or ""):
                return True
            if self._pm.match(pattern, parsed.netloc):
                return True
            base_url = f"{parsed.scheme}://{parsed.netloc}"
            if self._pm.match(pattern, base_url):
                return True
            if self._pm.match(pattern, base_url + "/*"):
                return True
        except Exception:
            return False
        return False

    def match_url_any(self, patterns: list[str], url: str) -> bool:
        return any(self.match_url(p, url) for p in patterns)


class CommandMatcher:
    """命令匹配器 - 仅支持 wildcard，全串锚定防注入."""

    def __init__(self):
        self._pm = PatternMatcher()

    def match_command(self, pattern: str, command: str) -> bool:
        """匹配命令字符串 (wildcard 模式，全串锚定)."""
        if not command:
            return False
        return self._pm.match(pattern, command)

    def match_command_any(self, patterns: list[str], command: str) -> bool:
        return any(self.match_command(p, command) for p in patterns)


# ----- 全局便捷函数 -----
_pattern_matcher = PatternMatcher()
_path_matcher = PathMatcher()
_url_matcher = URLMatcher()
_command_matcher = CommandMatcher()


def match_pattern(pattern: str, value: str) -> bool:
    return _pattern_matcher.match(pattern, value)


def match_path(pattern: str, path: str | Path) -> bool:
    return _path_matcher.match_path(pattern, path)


def match_url(pattern: str, url: str) -> bool:
    return _url_matcher.match_url(pattern, url)


def match_command(pattern: str, command: str) -> bool:
    return _command_matcher.match_command(pattern, command)


def build_command_allow_pattern(cmd: str) -> str:
    """构建匹配完整命令的通配符模式.

    Examples:
        "start chrome"   → start chrome *
        "npm install"    → npm install *
        "ls"             → ls *
    """
    return cmd.strip() + " *"


def contains_path(parent: str | Path, child: str | Path) -> bool:
    """子路径是否在父路径下（含路径穿越防护）.
    """
    import os
    try:
        rel = os.path.relpath(Path(child).resolve(), Path(parent).resolve())
        return not rel.startswith("..") and rel != ".."
    except (ValueError, OSError):
        return False


# ---------- 权限规则持久化 ----------


def _is_guard_baseline(permissions: dict[str, Any], tool_name: str) -> bool:
    """Phase-1：判断 tool baseline 是否为 ``guard``（含历史 ``ask`` 自动升级）。

    若 ``tools.<tool>`` 显式声明了 ``allow / deny / guard / ask``，按字面判定；
    否则回退到 ``defaults``。``ask`` 在 Phase-1 一律视为 ``guard``。
    """
    tools_cfg = permissions.get("tools") or {}
    raw = tools_cfg.get(tool_name) if isinstance(tools_cfg, dict) else None
    if isinstance(raw, str):
        norm = raw.strip().lower()
        if norm in ("guard", "ask"):
            return True
        if norm in ("allow", "deny"):
            return False
    raw_default = permissions.get("defaults", "guard")
    if isinstance(raw_default, str):
        norm = raw_default.strip().lower()
        return norm in ("guard", "ask")
    return True


def persist_permission_allow_rule(
    tool_name: str,
    tool_args: dict | str,
    *,
    permission_context: dict[str, Any] | None = None,
) -> bool:
    """用户选择「总是允许」时，将 allow 规则写入 config.yaml.

    For mcp_exec_command with a command arg, adds a wildcard pattern.
    For other tools, sets the tool to 'allow'.
    """
    if isinstance(tool_args, str):
        try:
            tool_args = json.loads(tool_args)
        except Exception:
            tool_args = {}

    logger.info(
        "[PermissionEngine] permission.persist.start tool=%s tool_args_type=%s tool_args=%s",
        tool_name,
        type(tool_args).__name__,
        str(tool_args)[:200],
    )

    try:
        from jiuwenclaw.agentserver.permissions.core import get_permission_engine
        from jiuwenclaw.agentserver.permissions.shell_ast import parse_shell_for_permission
        from jiuwenclaw.agentserver.permissions.tiered_policy import (
            evaluate_tiered_policy,
            evaluate_tiered_policy_detailed,
        )
        from jiuwenclaw.agentserver.permissions.models import PermissionLevel
        from jiuwenclaw.config import (
            _CONFIG_YAML_PATH,
            _load_yaml_round_trip,
            _dump_yaml_round_trip,
        )

        logger.debug("[PermissionEngine] permission.persist.config_path path=%s", _CONFIG_YAML_PATH)
        data = _load_yaml_round_trip(_CONFIG_YAML_PATH)
        permissions = data.get("permissions")
        if permissions is None:
            logger.warning(
                "[PermissionEngine] permission.persist.abort tool=%s reason=no_permissions_section",
                tool_name,
            )
            return False
        current_permission, current_matched_rule = evaluate_tiered_policy(
            permissions, tool_name, tool_args,
        )
        # Phase-1：``guard`` baseline 的非 shell 工具会让 tiered_policy 返回 ALLOW
        # （表示"无意见，交给 file_guard 裁决"）。这种 ALLOW 不能阻止 persist：
        # 用户的"总是允许"既可能是想把 tools.<name> 提为 allow，也可能是想把
        # file_guard 规则永久化。判定窗口放宽：只要 baseline 是 guard 视为可持久化。
        is_guard_baseline = _is_guard_baseline(permissions, tool_name)
        if current_permission != PermissionLevel.ASK and not is_guard_baseline:
            logger.warning(
                "[PermissionEngine] permission.persist.skip tool=%s reason=current_permission_not_ask current=%s",
                tool_name,
                current_permission.value,
            )
            return False
        if current_permission == PermissionLevel.DENY:
            logger.warning(
                "[PermissionEngine] permission.persist.skip tool=%s reason=current_permission_deny matched_rule=%s",
                tool_name,
                current_matched_rule,
            )
            return False

        if is_shell_permission_tool(tool_name):
            suggestions = _permission_suggestions_from_context(permission_context)
            if not suggestions:
                ask_subcommands = _ask_subcommands_from_context(permission_context)
                if not ask_subcommands:
                    _level, _rule, subcommand_results = evaluate_tiered_policy_detailed(
                        permissions,
                        tool_name,
                        tool_args,
                    )
                    ask_subcommands = _ask_subcommands_from_policy_result(subcommand_results)
                shell_ast_result = parse_shell_for_permission(extract_shell_command(tool_args))
                suggestions = build_permission_suggestions(
                    tool_name,
                    tool_args,
                    shell_ast_result=shell_ast_result,
                    ask_subcommands=ask_subcommands,
                    existing_patterns=_existing_allow_override_patterns(permissions),
                )
            persisted = _persist_tiered_approval_override_suggestions(permissions, suggestions)
            if persisted:
                logger.info(
                    "[PermissionEngine] permission.persist.write tool=%s target=approval_overrides persisted=true",
                    tool_name,
                )
            else:
                logger.warning(
                    "[PermissionEngine] permission.persist.skip tool=%s reason=no_safe_suggestion",
                    tool_name,
                )
                return False
        else:
            persisted = _persist_tiered_tool_allow(permissions, tool_name)
            logger.info(
                "[PermissionEngine] permission.persist.write tool=%s target=tools persisted=%s",
                tool_name,
                persisted,
            )

        _dump_yaml_round_trip(_CONFIG_YAML_PATH, data)
        logger.info("[PermissionEngine] permission.persist.write tool=%s target=config_yaml persisted=true", tool_name)

        verify_data = _load_yaml_round_trip(_CONFIG_YAML_PATH)
        engine = get_permission_engine()
        engine.update_config(verify_data.get("permissions", {}))
        logger.info("[PermissionEngine] permission.persist.reload tool=%s reloaded=true", tool_name)
        return persisted

    except Exception:
        logger.error("[PermissionEngine] permission.persist.failed tool=%s", tool_name, exc_info=True)
        return False


def _permission_suggestions_from_context(permission_context: dict | None) -> list[PermissionSuggestion]:
    if not isinstance(permission_context, dict):
        return []
    raw_patterns = permission_context.get("would_persist_patterns")
    if not isinstance(raw_patterns, list):
        return []

    seen: set[str] = set()
    suggestions: list[PermissionSuggestion] = []
    for item in raw_patterns:
        pattern = str(item or "").strip()
        if not pattern or pattern in seen:
            continue
        seen.add(pattern)
        scope = _infer_persisted_rule_scope(pattern)
        suggestions.append(PermissionSuggestion(
            tools=tuple(_SHELL_APPROVAL_TOOLS),
            match_type="command",
            pattern=pattern,
            action="allow",
            scope=scope,
            reason="permission_context.would_persist_patterns",
        ))
    return suggestions


def _infer_persisted_rule_scope(pattern: str) -> str:
    text = str(pattern or "").strip()
    if text.lower().startswith("re:"):
        return "regex"
    if text.endswith(" *"):
        return "head"
    return "exact"


def _ask_subcommands_from_context(permission_context: dict | None) -> list[str]:
    if not isinstance(permission_context, dict):
        return []
    raw = permission_context.get("ask_subcommands")
    if not isinstance(raw, list):
        return []
    return [item.strip() for item in raw if isinstance(item, str) and item.strip()]


def _ask_subcommands_from_policy_result(
    subcommand_results: list[tuple[str, Any, str]] | None,
) -> list[str]:
    if not subcommand_results:
        return []
    ask_subcommands: list[str] = []
    for text, permission, _matched_rule in subcommand_results:
        if not text:
            continue
        if permission == "ask" or getattr(permission, "value", None) == "ask":
            ask_subcommands.append(text)
    return ask_subcommands


def _persist_tiered_approval_override_suggestions(
    permissions: dict,
    suggestions: list[PermissionSuggestion],
) -> bool:
    if not suggestions:
        return False
    overrides = permissions.get("approval_overrides")
    if not isinstance(overrides, list):
        overrides = []
    _set_approval_overrides_after_rules(permissions, overrides)

    persisted_any = False
    for suggestion in suggestions:
        if _ensure_single_allow_override(
                overrides,
                pattern=suggestion.pattern,
                action=suggestion.action,
                scope=suggestion.scope,
        ):
            persisted_any = True
    return persisted_any


def _set_approval_overrides_after_rules(permissions: dict, overrides: list[Any]) -> None:
    if "approval_overrides" in permissions:
        permissions["approval_overrides"] = overrides
        return
    insert = getattr(permissions, "insert", None)
    if callable(insert):
        keys = list(permissions.keys())
        if "owner_scopes" in keys:
            index = keys.index("owner_scopes")
        elif "rules" in keys:
            index = keys.index("rules") + 1
        else:
            index = len(keys)
        insert(index, "approval_overrides", overrides)
        return
    permissions["approval_overrides"] = overrides


def _persist_tiered_tool_allow(permissions: dict, tool_name: str) -> bool:
    tools = permissions.get("tools")
    if not isinstance(tools, dict):
        tools = {}
        permissions["tools"] = tools
    if tools.get(tool_name) == "allow":
        return True
    tools[tool_name] = "allow"
    return True


def _ensure_single_allow_override(
    overrides: list[Any],
    *,
    pattern: str,
    action: str,
    scope: str = "head",
) -> bool:
    for existing in overrides:
        if not isinstance(existing, dict):
            continue
        existing_pattern = existing.get("pattern")
        existing_action = str(existing.get("action") or "").strip().lower()
        signature = _ApprovalOverrideSignature(
            pattern=pattern,
            existing_pattern=existing_pattern,
            existing_action=existing_action,
        )
        if _is_same_allow_override(signature):
            logger.info(
                "[PermissionEngine] permission.persist.skip reason=approval_override_exists pattern=%s",
                pattern,
            )
            return True

    overrides.append({
        "id": _build_approval_override_id(pattern),
        "pattern": pattern,
        "action": action,
        "scope": scope,
    })
    return True


def _is_same_allow_override(signature: _ApprovalOverrideSignature) -> bool:
    if signature.existing_pattern != signature.pattern:
        return False
    return signature.existing_action == "allow"


def _build_approval_override_id(pattern: str) -> str:
    raw_pattern = str(pattern or "").strip()
    digest = hashlib.sha256(raw_pattern.encode("utf-8")).hexdigest()[:12]
    collapsed = re.sub(r"[^a-zA-Z0-9]+", "_", raw_pattern).strip("_").lower()
    preview = collapsed[:32].strip("_") or "override"
    return f"user_allow_{preview}_{digest}"


def _existing_allow_override_patterns(permissions: dict) -> set[str]:
    patterns: set[str] = set()
    for section_name in ("rules", "approval_overrides"):
        raw = permissions.get(section_name)
        if not isinstance(raw, list):
            continue
        for item in raw:
            if not isinstance(item, dict):
                continue
            if str(item.get("action") or "").strip().lower() != "allow":
                continue
            pattern = item.get("pattern")
            if isinstance(pattern, str) and pattern:
                patterns.add(pattern)
    return patterns


def persist_external_directory_allow(paths: list[str]) -> None:
    """已废弃：兼容旧调用方，转发到 ``file_guard.persist_legacy_external_allow_paths``。

    Phase-1 起所有「外部路径总是允许」的写入都落到 ``permissions.file_guard.global``，
    不再写 ``permissions.external_directory``（后者只在加载时迁移）。
    """
    if not paths:
        return
    from jiuwenclaw.agentserver.permissions.file_guard import (
        persist_legacy_external_allow_paths,
    )
    logger.info(
        "[PermissionEngine] permission.persist.external.compat paths=%s target=file_guard.global",
        paths[:3],
    )
    persist_legacy_external_allow_paths(list(paths))


def persist_cli_trusted_directory(raw_path: str) -> dict[str, Any]:
    """CLI ``command.add_dir``：全局信任目录子树。

    Phase-1 起：路径维度写入 ``permissions.file_guard.global / trusted_exec_directory``
    （读 / 写 / 执行均放行），并在 ``tiered_policy`` 下追加 ``approval_overrides`` 让 shell
    命令字符串里出现该路径时也直接放行。

    ``remember`` 由调用方忽略；本函数始终落盘。
    """
    if not isinstance(raw_path, str) or not raw_path.strip():
        return {"ok": False, "error": "path is empty"}

    try:
        resolved = Path(raw_path.strip()).expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as e:
        return {"ok": False, "error": f"invalid path: {e}"}

    dir_norm = resolved.as_posix().rstrip("/")
    if not dir_norm:
        return {"ok": False, "error": "path resolves to empty"}

    try:
        from jiuwenclaw.agentserver.permissions.core import get_permission_engine
        from jiuwenclaw.agentserver.permissions.file_guard import (
            apply_cli_trusted_to_permissions_dict,
        )
        from jiuwenclaw.config import (
            _CONFIG_YAML_PATH,
            _load_yaml_round_trip,
            _dump_yaml_round_trip,
        )

        data = _load_yaml_round_trip(_CONFIG_YAML_PATH)
        permissions = data.get("permissions")
        if permissions is None:
            permissions = {}
            data["permissions"] = permissions

        apply_cli_trusted_to_permissions_dict(permissions, dir_norm)
        logger.info(
            "[PermissionEngine] permission.persist.cli_add_dir.file_guard.write path=%s targets=global+trusted_exec",
            dir_norm,
        )

        path_pattern = "re:^" + re.escape(dir_norm) + r"(?:$|/)"
        posix = dir_norm
        # 仅用正斜杠路径；反斜杠写入 YAML 双引号后易被解析成 \U 等非法正则转义，匹配改由 tiered 对 command 做 \→/ 归一化
        shell_pattern = "re:" + rf".*{re.escape(posix)}.*"

        suffix = hashlib.sha256(dir_norm.encode("utf-8")).hexdigest()[:16]
        shell_override_id = f"cli_trusted_shell_{suffix}"

        overrides = permissions.get("approval_overrides")
        if not isinstance(overrides, list):
            overrides = []
        _set_approval_overrides_after_rules(permissions, overrides)

        def _has_id(oid: str) -> bool:
            for r in overrides:
                if isinstance(r, dict) and r.get("id") == oid:
                    return True
            return False

        if not _has_id(shell_override_id):
            overrides.append({
                "id": shell_override_id,
                "pattern": shell_pattern,
                "action": "allow",
            })
            logger.info(
                "[PermissionEngine] permission.persist.cli_add_dir.override.write target=shell id=%s tools=%s",
                shell_override_id,
                sorted(SHELL_PERMISSION_TOOLS),
            )

        _dump_yaml_round_trip(_CONFIG_YAML_PATH, data)
        engine = get_permission_engine()
        engine.update_config(data.get("permissions", {}))
        return {
            "ok": True,
            "normalized": dir_norm,
            "path_pattern": path_pattern,
            "shell_pattern": shell_pattern,
            "tiered_overrides": True,
        }
    except Exception as e:  # noqa: BLE001
        logger.exception("[PermissionEngine] permission.persist.cli_add_dir.failed error=%s", e)
        return {"ok": False, "error": str(e)}
