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

from openjiuwen.harness.security.patterns import (
    merge_permission_allow_rule_into_permissions,
)

logger = logging.getLogger(__name__)


def _load_config_yaml_round_trip() -> tuple[Any, Any]:
    """Load config.yaml and return (data, yaml_path)."""
    from jiuwenswarm.common.config import _CONFIG_YAML_PATH, _load_yaml_round_trip

    data = _load_yaml_round_trip(_CONFIG_YAML_PATH)
    return data, _CONFIG_YAML_PATH


def _dump_config_yaml_round_trip(yaml_path: Any, data: Any) -> None:
    from jiuwenswarm.common.config import _dump_yaml_round_trip

    _dump_yaml_round_trip(yaml_path, data)


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


def _ensure_file_guard_dict(permissions: dict[str, Any]) -> dict[str, Any]:
    fg = permissions.get("file_guard")
    if not isinstance(fg, dict):
        fg = {}
        permissions["file_guard"] = fg
    fg["enabled"] = True
    paths = fg.get("paths")
    if not isinstance(paths, list):
        paths = []
        fg["paths"] = paths
    return fg


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
    """写入 / 更新一条 ``file_guard.paths``；返回是否有变更。

    优先调用 agent-core ``merge_file_guard_path_rule``；不可用时本地写入。
    """
    try:
        from openjiuwen.harness.security.patterns import merge_file_guard_path_rule

        merged, wrote = merge_file_guard_path_rule(
            permissions, path_norm, read=read, write=write, exec_=exec_,
        )
        # merge 返回副本；写回同一 permissions 引用供调用方 dump
        permissions.clear()
        permissions.update(merged)
        return wrote
    except ImportError:
        pass

    from ruamel.yaml.scalarstring import DoubleQuotedScalarString

    fg = _ensure_file_guard_dict(permissions)
    paths: list[Any] = fg["paths"]  # type: ignore[assignment]
    entry = {
        "path": DoubleQuotedScalarString(path_norm),
        "read": DoubleQuotedScalarString(read),
        "write": DoubleQuotedScalarString(write),
        "exec": DoubleQuotedScalarString(exec_),
        "match": DoubleQuotedScalarString("prefix"),
    }
    for i, existing in enumerate(paths):
        if not isinstance(existing, dict):
            continue
        existing_path = str(existing.get("path") or "").replace("\\", "/").rstrip("/")
        if existing_path != path_norm:
            continue
        if (
            existing.get("read") == read
            and existing.get("write") == write
            and existing.get("exec") == exec_
        ):
            return False
        paths[i] = {**existing, **entry}
        return True
    paths.append(entry)
    return True


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

    data, yaml_path = _load_config_yaml_round_trip()
    permissions = data.get("permissions")
    if not isinstance(permissions, dict):
        logger.warning(
            "[PermissionPersist] persist_permission_allow_rule.abort reason=no_permissions_section tool=%s",
            tool_name,
        )
        return False

    merged, ok = merge_permission_allow_rule_into_permissions(permissions, tool_name, tool_args)
    if not ok:
        return False
    data["permissions"] = merged
    _dump_config_yaml_round_trip(yaml_path, data)
    return True


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

    try:
        from openjiuwen.harness.security.patterns import merge_file_guard_access_allows

        data, yaml_path = _load_config_yaml_round_trip()
        permissions = _ensure_permissions_dict(data)
        access_list: list[tuple[str, str]] = []
        for i, path_str in enumerate(paths):
            act = "read"
            if actions is not None and i < len(actions) and actions[i]:
                act = str(actions[i])
            access_list.append((path_str, act))
        merged, wrote = merge_file_guard_access_allows(permissions, access_list)
        if wrote:
            data["permissions"] = merged
            _dump_config_yaml_round_trip(yaml_path, data)
        return
    except ImportError:
        pass

    data, yaml_path = _load_config_yaml_round_trip()
    permissions = _ensure_permissions_dict(data)
    wrote = False
    for i, path_str in enumerate(paths):
        path_norm = path_str.replace("\\", "/").rstrip("/")
        if not path_norm:
            continue
        act = "read"
        if actions is not None and i < len(actions) and actions[i]:
            act = str(actions[i]).strip().lower()
        if act == "write":
            read, write, exec_ = "allow", "allow", "ask"
        elif act == "exec":
            read, write, exec_ = "allow", "ask", "allow"
        else:
            read, write, exec_ = "allow", "ask", "ask"
        if _merge_file_guard_path_into_permissions(
            permissions, path_norm, read=read, write=write, exec_=exec_,
        ):
            wrote = True
    if wrote:
        _dump_config_yaml_round_trip(yaml_path, data)


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

    data, yaml_path = _load_config_yaml_round_trip()
    permissions = _ensure_permissions_dict(data)
    _merge_file_guard_path_into_permissions(
        permissions, dir_norm, read="allow", write="allow", exec_="ask",
    )
    _dump_config_yaml_round_trip(yaml_path, data)
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

    data, yaml_path = _load_config_yaml_round_trip()
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

    _dump_config_yaml_round_trip(yaml_path, data)
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
    "persist_permission_allow_rule",
]
