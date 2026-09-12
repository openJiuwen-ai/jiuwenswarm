"""权限配置落盘（宿主侧）。

openjiuwen 的 PermissionInterruptRail 在「总是允许」时会通过 ToolPermissionHost.persist_allow_rule
把合并后的整份 permissions 配置交给宿主写盘。与此同时，JiuWenSwarm 仍有 CLI/WS 的一些入口需要
「记住目录」等能力。

路径信任：
- ``/add-dir`` → ``file_guard.paths``（read/write allow，exec ask）
- HITL「总是允许」外部路径 → 触达路径本身 + 按当时 action 轴（read 不放开 write）
不再写入 ``external_directory`` 具名键，也不再写 path 类 ``approval_overrides``。
shell 命令维 ``approval_overrides`` 仍可保留。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

from openjiuwen.harness.security import (
    merge_file_guard_access_allows,
    merge_file_guard_path_rule,
    merge_permission_allow_rule_into_permissions,
)

logger = logging.getLogger(__name__)


def _ensure_permissions_dict(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    permissions = data.get("permissions")
    if permissions is None:
        permissions = {}
        data["permissions"] = permissions
    if not isinstance(permissions, dict):
        permissions = {}
        data["permissions"] = permissions
    return permissions


def _ensure_approval_overrides_list(permissions: dict[str, Any]) -> list[dict[str, Any]]:
    overrides = permissions.get("approval_overrides")
    if not isinstance(overrides, list):
        overrides = []
        permissions["approval_overrides"] = overrides
    return [i for i in overrides if isinstance(i, dict)]


def _has_override_id(overrides: list[dict[str, Any]], oid: str) -> bool:
    return any(i.get("id") == oid for i in overrides)


def _append_override_if_missing(
    overrides: list[dict[str, Any]],
    *,
    oid: str,
    tools: list[str],
    match_type: str,
    pattern: str,
    action: str,
    source: str,
) -> None:
    if _has_override_id(overrides, oid):
        return
    overrides.append(
        {
            "id": oid,
            "tools": tools,
            "match_type": match_type,
            "pattern": pattern,
            "action": action,
            "source": source,
        }
    )


def _merge_file_guard_path_into_permissions(
    permissions: dict[str, Any],
    path_norm: str,
    *,
    read: str = "allow",
    write: str = "allow",
    exec_: str = "ask",
) -> bool:
    """写入 / 更新一条 ``file_guard.paths``；返回是否有变更。"""
    merged, wrote = merge_file_guard_path_rule(
        permissions, path_norm, read=read, write=write, exec_=exec_,
    )
    # merge 返回副本；写回同一 permissions 引用供调用方 dump
    permissions.clear()
    permissions.update(merged)
    return wrote


def build_command_allow_pattern(cmd: str) -> str:
    """构建匹配完整命令的通配符模式."""
    return cmd.strip() + " *"


def _normalize_tool_args(tool_args: Any) -> dict[str, Any]:
    if isinstance(tool_args, dict):
        return tool_args
    if isinstance(tool_args, bytes):
        try:
            tool_args = tool_args.decode("utf-8", errors="ignore")
        except Exception:
            return {}
    if isinstance(tool_args, str):
        s = tool_args.strip()
        if not s:
            return {}
        try:
            parsed = json.loads(s)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def persist_permission_allow_rule(tool_name: str, tool_args: dict | str) -> bool:
    """用户选择「总是允许」时，将 allow 规则写入 config.yaml 的 permissions 段。"""
    tool_args = _normalize_tool_args(tool_args)

    from jiuwenswarm.common.config import update_config

    success = False

    def _mutate(data):
        nonlocal success
        permissions = data.get("permissions")
        if not isinstance(permissions, dict):
            logger.warning(
                "[PermissionPersist] persist_permission_allow_rule.abort reason=no_permissions_section tool=%s",
                tool_name,
            )
            return None
        merged, success = merge_permission_allow_rule_into_permissions(permissions, tool_name, tool_args)
        if not success:
            return None
        data["permissions"] = merged
        return data

    update_config(_mutate)
    return success


def persist_exact_permission_allow_rule(
    tool_name: str,
    tool_args: dict | str,
    ask_accesses: tuple[tuple[str, str], ...] = (),
    *,
    session_id: str | None = None,
    workspace_root: str | Path | None = None,
) -> bool:
    """Validate current layers and persist only this approval's User increment."""
    from copy import deepcopy

    from openjiuwen.harness.security.file_guard import build_file_guard_checker
    from openjiuwen.harness.security.models import PermissionLevel
    from openjiuwen.harness.security.tiered_policy import evaluate_tiered_policy

    from jiuwenswarm.common.utils import get_workspace_dir
    from jiuwenswarm.agents.harness.common.rails.permissions.permission_compose import (
        compose_host_effective_permissions,
    )
    from jiuwenswarm.agents.harness.common.rails.permissions import permissions_layers as layers

    name = str(tool_name or "").strip()
    args = _normalize_tool_args(tool_args)
    accesses = _normalize_exact_accesses(ask_accesses)
    if not name or accesses is None:
        return False

    try:
        root = workspace_root if workspace_root is not None else get_workspace_dir()
        with layers.permission_storage_lock(session_id):
            global_perms, user, session = layers.read_permission_layers_locked(session_id)

            def compose(user_layer):
                return compose_host_effective_permissions(
                    global_permissions=global_perms,
                    user_permissions=user_layer,
                    session_permissions=session,
                )

            effective = compose(user)
            level, _ = evaluate_tiered_policy(effective, name, args)
            checker = build_file_guard_checker(effective, workspace_root=root, trusted_dirs=())
            file_result = checker.evaluate(name, args) if checker is not None else None
            if level == PermissionLevel.DENY or (
                file_result is not None and file_result.permission == PermissionLevel.DENY
            ):
                return False
            current_asks = set(checker.collect_ask_accesses(name, args) if checker else ())
            if not current_asks.issubset(set(accesses)):
                return False

            updated = deepcopy(user)
            if level == PermissionLevel.ASK:
                merged, applied = merge_permission_allow_rule_into_permissions(effective, name, args)
                if not applied:
                    return False
                if (merged.get("tools") or {}).get(name) != (effective.get("tools") or {}).get(name):
                    allow = list(updated.get("allow_tools") or [])
                    if name not in allow:
                        allow.append(name)
                    updated["allow_tools"] = allow
                previous = effective.get("approval_overrides") or []
                additions = [item for item in merged.get("approval_overrides") or [] if item not in previous]
                if additions:
                    updated["approval_overrides"] = list(updated.get("approval_overrides") or []) + additions

            if current_asks:
                updated, applied = merge_file_guard_access_allows(updated, sorted(current_asks))
                if not applied:
                    return False

            # A remembered grant must work without Session and must not be masked.
            proposed = compose_host_effective_permissions(
                global_permissions=global_perms,
                user_permissions=updated,
                session_permissions={},
            )
            proposed_level, _ = evaluate_tiered_policy(proposed, name, args)
            proposed_checker = build_file_guard_checker(proposed, workspace_root=root, trusted_dirs=())
            proposed_file = proposed_checker.evaluate(name, args) if proposed_checker else None
            if proposed_level != PermissionLevel.ALLOW or (
                proposed_file is not None and proposed_file.permission != PermissionLevel.ALLOW
            ):
                return False
            if updated == user:
                return True
            return layers.save_user_permissions_locked(updated)
    except Exception:
        logger.exception("[PermissionPersist] exact permission persist failed")
        return False


def _normalize_exact_accesses(
    accesses: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...] | None:
    if not isinstance(accesses, tuple):
        return None
    normalized: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for access in accesses:
        if not isinstance(access, tuple) or len(access) != 2:
            return None
        path, action = access
        path_norm = str(path or "").replace("\\", "/").rstrip("/")
        action_norm = str(action or "").strip().lower()
        if not path_norm or action_norm not in {"read", "write", "exec"}:
            return None
        item = (path_norm, action_norm)
        if item not in seen:
            seen.add(item)
            normalized.append(item)
    return tuple(normalized)


def persist_external_directory_allow(
    paths: list[str],
    *,
    actions: list[str] | None = None,
) -> None:
    """用户选择「总是允许」外部路径时，写入 ``file_guard.paths``。

    - 写入触达路径本身（**不上卷父目录**）
    - 缺省按 read 轴 allow（write/exec=ask）；可用 ``actions`` 与 paths 对齐传入 write/exec
    函数名保留兼容；不再写入 ``external_directory`` 具名键。
    """
    if not paths:
        return

    from jiuwenswarm.common.config import update_config

    access_list: list[tuple[str, str]] = []
    for i, path_str in enumerate(paths):
        act = "read"
        if actions is not None and i < len(actions) and actions[i]:
            act = str(actions[i])
        access_list.append((path_str, act))

    def _mutate(data):
        merged, wrote = merge_file_guard_access_allows(_ensure_permissions_dict(data), access_list)
        if not wrote:
            return None
        data["permissions"] = merged
        return data

    update_config(_mutate)


def persist_cli_trusted_directory(raw_path: str) -> dict[str, Any]:
    """CLI ``command.add_dir``：全局信任目录子树。

    写入 ``permissions.file_guard.paths``：``read/write: allow``，``exec: ask``。
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

    from jiuwenswarm.common.config import update_config

    def _mutate(data):
        _merge_file_guard_path_into_permissions(
            _ensure_permissions_dict(data), dir_norm, read="allow", write="allow", exec_="ask",
        )
        return data

    update_config(_mutate)
    logger.info(
        "[PermissionPersist] cli_add_dir.file_guard path=%s read=allow write=allow exec=ask",
        dir_norm,
    )
    return {
        "ok": True,
        "normalized": dir_norm,
        "file_guard": True,
    }


def persist_cli_trusted_directory_with_overrides(raw_path: str) -> dict[str, Any]:
    """CLI ``command.add_dir``：信任目录 + shell 命令维 approval_overrides。

    写入：
    - ``permissions.file_guard.paths``：目录 read/write allow，exec ask
    - ``permissions.approval_overrides``：仅 shell ``match_type: command``（不再写 path 类）
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

    from jiuwenswarm.common.config import update_config

    tiered = False
    shell_pattern = ""

    def _mutate(data):
        nonlocal tiered, shell_pattern
        permissions = _ensure_permissions_dict(data)
        _merge_file_guard_path_into_permissions(
            permissions, dir_norm, read="allow", write="allow", exec_="ask",
        )

        shell_pattern = "re:" + rf".*{re.escape(dir_norm)}.*"
        schema_key = str(permissions.get("schema") or permissions.get("version") or "").strip().lower()
        tiered = schema_key in {"tiered_policy", "v_cc", "v4.2", ""}

        suffix = hashlib.sha256(dir_norm.encode("utf-8")).hexdigest()[:16]
        shell_override_id = f"cli_trusted_shell_{suffix}"

        if tiered:
            overrides = _ensure_approval_overrides_list(permissions)
            # 写回 list（_ensure 可能过滤）
            permissions["approval_overrides"] = overrides
            shell_tools = sorted({"bash", "mcp_exec_command", "create_terminal"})
            _append_override_if_missing(
                overrides,
                oid=shell_override_id,
                tools=shell_tools,
                match_type="command",
                pattern=shell_pattern,
                action="allow",
                source="cli_add_dir",
            )

        return data

    update_config(_mutate)
    return {
        "ok": True,
        "normalized": dir_norm,
        "shell_pattern": shell_pattern,
        "file_guard": True,
        "tiered_overrides": tiered,
    }


__all__ = [
    "build_command_allow_pattern",
    "persist_cli_trusted_directory",
    "persist_cli_trusted_directory_with_overrides",
    "persist_external_directory_allow",
    "persist_exact_permission_allow_rule",
    "persist_permission_allow_rule",
]
