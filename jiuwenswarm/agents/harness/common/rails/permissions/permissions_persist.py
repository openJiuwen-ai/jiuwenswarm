"""权限配置落盘（宿主侧）。

openjiuwen 的 PermissionInterruptRail 在「总是允许」时会通过 ToolPermissionHost.persist_allow_rule
把合并后的整份 permissions 配置交给宿主写盘。与此同时，JiuWenSwarm 仍有 CLI/WS 的一些入口需要
「记住目录」等能力。

「会话内记住」走 ``persist_session_allow_rule``：只把相对磁盘（及已有 overlay）
新增/抬升的 ``file_guard.paths`` 与 ``approval_overrides`` 增量写入
``{sessions}/{session_id}/session_permissions.yaml``，不落 tools/defaults 等全量规则。
``get_permissions_with_session_overlay`` 在每次 first_check 快照里把 overlay 叠回磁盘配置
（不写 config.yaml）。

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
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

from openjiuwen.harness.security.patterns import (
    merge_file_guard_path_rule,
    merge_permission_allow_rule_into_permissions,
)

logger = logging.getLogger(__name__)

_SESSION_OVERLAY_FILENAME = "session_permissions.yaml"
_SESSION_ID_MAX_LEN = 128
_session_overlay_lock = threading.Lock()
_AXIS_RANK = {"deny": 0, "ask": 1, "allow": 2}


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


def _current_session_id() -> str:
    try:
        from jiuwenswarm.agents.harness.common.channel_runtime_context import CURRENT_SESSION_ID

        return (CURRENT_SESSION_ID.get() or "").strip()
    except Exception:
        return ""


def _overlay_session_id(session_id: str | None) -> str:
    """Prefer an explicit rail/session id; ContextVar is a fallback only."""
    sid = (session_id or "").strip()
    if sid:
        return sid
    return _current_session_id()


def _sanitize_session_id(session_id: str) -> str:
    sid = re.sub(r"[^A-Za-z0-9_.-]", "_", (session_id or "").strip())[:_SESSION_ID_MAX_LEN]
    if not sid or sid in {".", ".."}:
        return ""
    return sid


def session_permissions_overlay_path(session_id: str) -> Path | None:
    """Return ``{sessions}/{sid}/session_permissions.yaml`` if the id is path-safe."""
    sid = _sanitize_session_id(session_id)
    if not sid:
        return None
    from jiuwenswarm.common.utils import get_agent_sessions_dir

    root = get_agent_sessions_dir().resolve()
    path = (root / sid / _SESSION_OVERLAY_FILENAME).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    return path


def _norm_path(value: Any) -> str:
    return str(value or "").replace("\\", "/").rstrip("/")


def _normalize_axis(value: Any) -> str:
    text = str(value or "ask").strip().lower()
    if text in _AXIS_RANK:
        return text
    return "ask"


def _axis_escalated(old: Any, new: Any) -> bool:
    return _AXIS_RANK[_normalize_axis(new)] > _AXIS_RANK[_normalize_axis(old)]


def _file_guard_paths_by_key(perms: dict[str, Any]) -> dict[str, dict[str, Any]]:
    fg = perms.get("file_guard")
    if not isinstance(fg, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    paths = fg.get("paths")
    if not isinstance(paths, list):
        return out
    for entry in paths:
        if not isinstance(entry, dict):
            continue
        key = _norm_path(entry.get("path"))
        if key:
            out[key] = entry
    return out


def _as_str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(x) for x in value]
    return []


def _override_same(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return (
        str(left.get("id") or "") == str(right.get("id") or "")
        and _as_str_list(left.get("tools")) == _as_str_list(right.get("tools"))
        and str(left.get("match_type") or "") == str(right.get("match_type") or "")
        and str(left.get("pattern") or "") == str(right.get("pattern") or "")
        and str(left.get("action") or "") == str(right.get("action") or "")
    )


def _disk_permissions() -> dict[str, Any]:
    try:
        from jiuwenswarm.common.config import get_config

        cfg = get_config()
    except Exception:
        return {}
    perms = cfg.get("permissions") if isinstance(cfg, dict) else {}
    return perms if isinstance(perms, dict) else {}


def extract_session_overlay_delta(
    baseline: dict[str, Any] | None,
    merged: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return only file_guard paths / approval_overrides that are new vs baseline."""
    base = baseline if isinstance(baseline, dict) else {}
    new = merged if isinstance(merged, dict) else {}
    delta: dict[str, Any] = {}

    base_paths = _file_guard_paths_by_key(base)
    delta_paths: list[dict[str, Any]] = []
    for key, entry in _file_guard_paths_by_key(new).items():
        old = base_paths.get(key)
        new_read = _normalize_axis(entry.get("read"))
        new_write = _normalize_axis(entry.get("write"))
        new_exec = _normalize_axis(entry.get("exec"))
        new_match = str(entry.get("match") or "prefix")
        if old is None:
            changed = True
        else:
            changed = (
                _axis_escalated(old.get("read"), new_read)
                or _axis_escalated(old.get("write"), new_write)
                or _axis_escalated(old.get("exec"), new_exec)
            )
        if not changed:
            continue
        delta_paths.append(
            {
                "path": key,
                "read": new_read,
                "write": new_write,
                "exec": new_exec,
                "match": new_match,
            }
        )
    if delta_paths:
        delta["file_guard"] = {"paths": delta_paths}

    base_overrides: dict[str, dict[str, Any]] = {}
    raw_base_ov = base.get("approval_overrides")
    if isinstance(raw_base_ov, list):
        for item in raw_base_ov:
            if isinstance(item, dict) and item.get("id"):
                base_overrides[str(item.get("id"))] = item
    delta_overrides: list[dict[str, Any]] = []
    raw_new_ov = new.get("approval_overrides")
    if isinstance(raw_new_ov, list):
        for item in raw_new_ov:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            oid = str(item.get("id"))
            old_item = base_overrides.get(oid)
            if old_item is None or not _override_same(old_item, item):
                delta_overrides.append(dict(item))
    if delta_overrides:
        delta["approval_overrides"] = delta_overrides
    return delta


def _compact_session_overlay(data: dict[str, Any]) -> dict[str, Any]:
    """Keep only incremental overlay keys; drop tools/defaults/enabled copies."""
    out: dict[str, Any] = {}
    fg = data.get("file_guard")
    if isinstance(fg, dict):
        raw_paths = fg.get("paths")
        paths = [
            {
                "path": _norm_path(p.get("path")),
                "read": _normalize_axis(p.get("read")),
                "write": _normalize_axis(p.get("write")),
                "exec": _normalize_axis(p.get("exec")),
                "match": str(p.get("match") or "prefix"),
            }
            for p in (raw_paths if isinstance(raw_paths, list) else [])
            if isinstance(p, dict) and _norm_path(p.get("path"))
        ]
        if paths:
            out["file_guard"] = {"paths": paths}
    raw_ov = data.get("approval_overrides")
    if isinstance(raw_ov, list):
        overrides = [dict(i) for i in raw_ov if isinstance(i, dict) and i.get("id")]
        if overrides:
            out["approval_overrides"] = overrides
    return out


def apply_session_permissions_overlay(
    base: dict[str, Any] | None,
    overlay: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge session overlay onto a disk permissions snapshot.

    Overlay wins on ``file_guard.paths`` (escalate toward allow) and
    ``approval_overrides`` (by ``id``). Disk ``file_guard.defaults`` stay.
    """
    merged: dict[str, Any] = deepcopy(base) if isinstance(base, dict) else {}
    if not isinstance(overlay, dict) or not overlay:
        return merged

    ov_fg = overlay.get("file_guard")
    if isinstance(ov_fg, dict):
        paths = ov_fg.get("paths")
        if isinstance(paths, list):
            for entry in paths:
                if not isinstance(entry, dict):
                    continue
                path = str(entry.get("path") or "")
                if not path:
                    continue
                merged, _wrote = merge_file_guard_path_rule(
                    merged,
                    path,
                    read=str(entry.get("read") or "allow"),
                    write=str(entry.get("write") or "ask"),
                    exec_=str(entry.get("exec") or "ask"),
                    match=str(entry.get("match") or "prefix"),
                )
        if ov_fg.get("enabled") is True:
            fg = merged.get("file_guard")
            if isinstance(fg, dict):
                fg["enabled"] = True

    ov_overrides = overlay.get("approval_overrides")
    if isinstance(ov_overrides, list):
        existing = merged.get("approval_overrides")
        if not isinstance(existing, list):
            existing = []
        by_id: dict[str, dict[str, Any]] = {}
        no_id: list[dict[str, Any]] = []
        for item in existing:
            if not isinstance(item, dict):
                continue
            oid = item.get("id")
            if oid:
                by_id[str(oid)] = item
            else:
                no_id.append(item)
        for item in ov_overrides:
            if not isinstance(item, dict):
                continue
            oid = item.get("id")
            if oid:
                by_id[str(oid)] = item
            else:
                no_id.append(item)
        merged["approval_overrides"] = list(by_id.values()) + no_id

    ov_ext = overlay.get("external_directory")
    if isinstance(ov_ext, dict):
        base_ext = merged.get("external_directory")
        if not isinstance(base_ext, dict):
            base_ext = {}
        else:
            base_ext = dict(base_ext)
        base_ext.update(ov_ext)
        merged["external_directory"] = base_ext
    return merged


def load_session_permissions_overlay(session_id: str) -> dict[str, Any]:
    path = session_permissions_overlay_path(session_id)
    if path is None or not path.is_file():
        return {}
    try:
        from jiuwenswarm.common.config import _load_yaml_round_trip

        data = _load_yaml_round_trip(path)
    except Exception:
        logger.warning(
            "[PermissionPersist] load_session_overlay.failed path=%s",
            path,
            exc_info=True,
        )
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def write_session_permissions_overlay(session_id: str, overlay: dict[str, Any]) -> bool:
    path = session_permissions_overlay_path(session_id)
    if path is None:
        return False
    if not isinstance(overlay, dict):
        overlay = {}
    try:
        from jiuwenswarm.common.config import _dump_yaml_round_trip

        with _session_overlay_lock:
            existing = load_session_permissions_overlay(session_id)
            merged = apply_session_permissions_overlay(existing, overlay)
            compact = _compact_session_overlay(merged)
            path.parent.mkdir(parents=True, exist_ok=True)
            _dump_yaml_round_trip(path, compact)
        return True
    except Exception:
        logger.warning(
            "[PermissionPersist] write_session_overlay.failed path=%s",
            path,
            exc_info=True,
        )
        return False


def persist_session_allow_rule(
    permissions: dict[str, Any],
    session_id: str | None = None,
) -> bool:
    """Incrementally persist session overlay for this session.

    ``permissions`` is the rail's already-merged snapshot (disk ∪ previous
    overlay ∪ this remember). Only paths/overrides that are new or escalated
    relative to disk ∪ existing overlay are written.

    Prefer the explicit ``session_id`` from the rail (``ctx.session``). The
    request ContextVar is often empty on interrupt resume.
    """
    sid = _overlay_session_id(session_id)
    if not sid:
        logger.warning(
            "[PermissionPersist] persist_session_allow_rule.abort reason=no_session_id"
        )
        return False
    if not isinstance(permissions, dict):
        return False
    existing = load_session_permissions_overlay(sid)
    baseline = apply_session_permissions_overlay(_disk_permissions(), existing)
    delta = extract_session_overlay_delta(baseline, permissions)
    if not delta:
        logger.info(
            "[PermissionPersist] persist_session_allow_rule.noop session=%s",
            sid,
        )
        return True
    ok = write_session_permissions_overlay(sid, delta)
    logger.info(
        "[PermissionPersist] persist_session_allow_rule session=%s ok=%s keys=%s",
        sid,
        ok,
        sorted(delta.keys()),
    )
    return ok


def get_permissions_with_session_overlay(
    base: dict[str, Any] | None = None,
    *,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Disk permissions ∪ current session overlay. Used as rail snapshot."""
    if base is None:
        from jiuwenswarm.common.config import get_config

        cfg = get_config()
        base = cfg.get("permissions") if isinstance(cfg, dict) else {}
    if not isinstance(base, dict):
        base = {}
    overlay = load_session_permissions_overlay(_overlay_session_id(session_id))
    return apply_session_permissions_overlay(base, overlay)


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
    "apply_session_permissions_overlay",
    "build_command_allow_pattern",
    "extract_session_overlay_delta",
    "get_permissions_with_session_overlay",
    "load_session_permissions_overlay",
    "persist_cli_trusted_directory",
    "persist_cli_trusted_directory_with_overrides",
    "persist_external_directory_allow",
    "persist_permission_allow_rule",
    "persist_session_allow_rule",
    "session_permissions_overlay_path",
    "write_session_permissions_overlay",
]
