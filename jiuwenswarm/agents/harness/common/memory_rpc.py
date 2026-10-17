# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from jiuwenswarm.agents.harness.common.memory import (
    get_memory_manager,
    is_memory_enabled,
)
from jiuwenswarm.agents.harness.common.memory.config import (
    is_agent_mode,
    is_auto_memory_enabled,
    is_proactive_memory,
)
from jiuwenswarm.agents.harness.common.memory.external_memory_config import (
    get_memory_engine,
    is_external_memory_allowed,
    get_external_memory_config,
)
from jiuwenswarm.agents.harness.common.rails.project_memory import (
    clear_project_memory_cache,
    discover_and_load_memory_files,
    get_large_memory_files,
)
from jiuwenswarm.common.coding_memory_paths import resolve_project_coding_memory_dir
from jiuwenswarm.common.config import get_config
from jiuwenswarm.common.mode_matrix import is_code_profile_mode
from jiuwenswarm.common.utils import get_agent_workspace_dir

logger = logging.getLogger(__name__)


def _is_forbidden_enabled(config: dict[str, Any] | None) -> bool:
    mem_cfg = (config or {}).get("memory", {}) if isinstance(config, dict) else {}
    forbidden = mem_cfg.get("forbidden_memory_definition", {})
    return bool(forbidden.get("enabled", False))


def _get_coding_memory_dir(workspace: str, project_dir: str | None = None) -> str:
    """Resolve coding memory below the agent-owned system workspace.

    ``workspace`` is retained for the RPC interface because it is also used
    for project-memory operations. Coding memory is agent-owned data, though,
    so it must not follow a project workspace supplied by the caller.
    """
    return resolve_project_coding_memory_dir(
        agent_workspace_dir=get_agent_workspace_dir(),
        project_dir=project_dir,
    )


def _get_allowed_dirs(workspace: str, project_dir: str | None = None) -> list[str]:
    allowed_dirs = [
        os.path.join(workspace, "memory"),
        _get_coding_memory_dir(workspace, project_dir),
        os.path.join(workspace, ".jiuwen"),
        os.path.expanduser("~/.jiuwen"),
    ]
    if project_dir:
        allowed_dirs.append(project_dir)
        allowed_dirs.append(os.path.join(project_dir, ".jiuwen"))
    return [os.path.normpath(d) for d in allowed_dirs]


def _is_in_allowed_dirs(abs_path: str, workspace: str, project_dir: str | None = None) -> bool:
    # On Windows, path comparison should be case-insensitive
    if os.name == "nt":
        abs_path_lower = abs_path.lower()
        for allowed_dir in _get_allowed_dirs(workspace, project_dir):
            allowed_dir_lower = allowed_dir.lower()
            if abs_path_lower == allowed_dir_lower or abs_path_lower.startswith(allowed_dir_lower + os.sep):
                return True
        return False
    else:
        for allowed_dir in _get_allowed_dirs(workspace, project_dir):
            if abs_path == allowed_dir or abs_path.startswith(allowed_dir + os.sep):
                return True
        return False


def _get_runtime_memory_dirs(workspace: str, project_dir: str | None = None) -> list[str]:
    """运行时记忆目录（agent 自动写入，对用户只读）。

    使用父目录以覆盖所有项目子目录：
    - <workspace>/memory                              -> agent mode auto memory
    - <agent-workspace>/coding_memory                 -> code mode coding memory
      （含各项目子目录）
    """
    return [
        os.path.normpath(os.path.join(workspace, "memory")),
        os.path.normpath(os.path.join(get_agent_workspace_dir(), "coding_memory")),
    ]


def _is_runtime_memory_path(abs_path: str, workspace: str, project_dir: str | None = None) -> bool:
    """abs_path 是否落在运行时记忆目录内（auto/coding memory 文件，禁止手动编辑）。"""
    if os.name == "nt":
        abs_lower = abs_path.lower()
        for d in _get_runtime_memory_dirs(workspace, project_dir):
            d_lower = d.lower()
            if abs_lower == d_lower or abs_lower.startswith(d_lower + os.sep):
                return True
        return False
    for d in _get_runtime_memory_dirs(workspace, project_dir):
        if abs_path == d or abs_path.startswith(d + os.sep):
            return True
    return False


def _validate_edit_path(raw_path: str, workspace: str, project_dir: str | None = None) -> tuple[bool, str]:
    normalized = raw_path.replace("\\", "/")
    expanded = os.path.expanduser(normalized)

    if os.path.isabs(expanded):
        abs_path = os.path.normpath(expanded)
    else:
        workspace_resolved = os.path.normpath(os.path.abspath(os.path.join(workspace, expanded)))
        abs_path = workspace_resolved
        if project_dir:
            project_resolved = os.path.normpath(os.path.abspath(os.path.join(project_dir, expanded)))
            if _is_in_allowed_dirs(project_resolved, workspace, project_dir):
                abs_path = project_resolved

    # 运行时记忆（auto/coding memory）只读，禁止手动编辑
    if _is_runtime_memory_path(abs_path, workspace, project_dir):
        return (False, "runtime memory is read-only, manual editing is not allowed")

    if _is_in_allowed_dirs(abs_path, workspace, project_dir):
        return (True, abs_path)

    basename = os.path.basename(abs_path)
    if basename in ("JIUWENSWARM.md", "JIUWENSWARM.local.md"):
        parent = os.path.dirname(abs_path)
        workspace_norm = os.path.normpath(workspace)
        if os.name == "nt":
            if parent.lower() == workspace_norm.lower():
                return (True, abs_path)
            if project_dir and parent.lower() == os.path.normpath(project_dir).lower():
                return (True, abs_path)
        else:
            if parent == workspace_norm:
                return (True, abs_path)
            if project_dir and parent == os.path.normpath(project_dir):
                return (True, abs_path)

    return (False, "path not in allowed memory directories")


def _classify_memory_file(path: str, workspace: str) -> str:
    if "coding_memory" in path:
        return "coding"
    parts = Path(path).parts
    if "memory" in parts:
        return "auto"
    if ".jiuwen" in parts:
        return "project"
    if path.endswith(".local.md"):
        return "local"
    jiuwen_dir = os.path.expanduser("~/.jiuwen")
    if path.startswith(jiuwen_dir):
        return "user"
    return "project"


def _is_creatable_memory_file(path: str) -> bool:
    """Whether a validated missing path is a user-managed memory entrypoint."""
    return os.path.basename(path) in ("JIUWENSWARM.md", "JIUWENSWARM.local.md")


def _relative_path(abs_path: str, workspace: str, project_dir: str | None = None) -> str:
    abs_path_norm = os.path.normpath(abs_path)
    if project_dir:
        project_dir_norm = os.path.normpath(project_dir)
        # On Windows, use case-insensitive comparison
        if os.name == "nt":
            abs_lower = abs_path_norm.lower()
            proj_lower = project_dir_norm.lower()
            if abs_lower.startswith(proj_lower + os.sep) or abs_lower == proj_lower:
                try:
                    return os.path.relpath(abs_path, project_dir)
                except ValueError:
                    pass
        else:
            if abs_path_norm.startswith(project_dir_norm + os.sep) or abs_path_norm == project_dir_norm:
                try:
                    return os.path.relpath(abs_path, project_dir)
                except ValueError:
                    pass
    workspace_norm = os.path.normpath(workspace)
    # On Windows, use case-insensitive comparison
    if os.name == "nt":
        abs_lower = abs_path_norm.lower()
        ws_lower = workspace_norm.lower()
        if abs_lower.startswith(ws_lower + os.sep) or abs_lower == ws_lower:
            try:
                return os.path.relpath(abs_path, workspace)
            except ValueError:
                pass
    else:
        if abs_path_norm.startswith(workspace_norm + os.sep) or abs_path_norm == workspace_norm:
            try:
                return os.path.relpath(abs_path, workspace)
            except ValueError:
                pass
    return abs_path


def _safe_stat(path: str) -> dict[str, Any]:
    try:
        s = Path(path).stat()
        return {"size": s.st_size, "mtime": s.st_mtime}
    except OSError:
        return {"size": 0, "mtime": 0}


def _count_lines(path: str) -> int:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def _scan_md_files(directory: str, kind: str, workspace: str, project_dir: str | None = None) -> list[dict[str, Any]]:
    if not os.path.isdir(directory):
        return []
    results: list[dict[str, Any]] = []
    for entry in sorted(os.scandir(directory), key=lambda e: e.name):
        if entry.is_file() and entry.name.endswith(".md"):
            stat = _safe_stat(entry.path)
            results.append({
                "path": entry.path,
                "relative_path": _relative_path(entry.path, workspace, project_dir),
                "kind": kind,
                "exists": True,
                "size": stat["size"],
                "mtime": stat["mtime"],
                "lines": _count_lines(entry.path),
            })
    return results


def _is_code_mode(mode: str) -> bool:
    # code profile 族：旧 "code"/"code.*" 前缀 + 新 canonical agent.code.* /
    # team.code.*（复用 mode_matrix 谓词，与 memory.config 的归一逻辑一致）。
    return is_code_profile_mode(mode) or (mode or "").startswith("code")


def _is_agent_mode(mode: str) -> bool:
    return is_agent_mode(mode)


async def handle_memory_list(
    workspace: str,
    mode: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    include_project = params.get("include_project", True)
    additional_dirs = params.get("additional_directories")
    project_dir = params.get("project_dir")

    files: list[dict[str, Any]] = []
    seen_paths: set[str] = set()

    if include_project:
        discover_workspace = project_dir or workspace
        project_files = discover_and_load_memory_files(
            workspace=discover_workspace,
            target_path=discover_workspace,
            additional_directories=additional_dirs,
        )
        for f in project_files:
            stat = _safe_stat(f.path)
            files.append({
                "path": f.path,
                "relative_path": _relative_path(f.path, workspace, project_dir),
                "kind": f.kind,
                "exists": True,
                "size": stat["size"],
                "mtime": stat["mtime"],
                "lines": f.content.count("\n") + 1 if f.content else 0,
            })
            seen_paths.add(f.path)

    # Auto-memory: unified with coding memory directory
    # Scan coding memory directory (auto-memory files are also stored there)
    coding_dir = _get_coding_memory_dir(workspace, project_dir)
    for item in _scan_md_files(coding_dir, "coding", workspace, project_dir):
        if item["path"] not in seen_paths:
            files.append(item)
            seen_paths.add(item["path"])

    return {"files": files, "mode": mode}


async def handle_memory_edit(
    workspace: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    raw_path = params.get("path", "")
    project_dir = params.get("project_dir")
    is_valid, resolved = _validate_edit_path(raw_path, workspace, project_dir)

    if not is_valid:
        return {
            "path": raw_path,
            "exists": False,
            "content_preview": "",
            "kind": "unknown",
            "editable": False,
            "reason": resolved,
        }

    exists = Path(resolved).is_file()
    content_preview = ""
    kind = _classify_memory_file(resolved, workspace)

    if not exists:
        editable = _is_creatable_memory_file(resolved)
        return {
            "path": resolved,
            "exists": False,
            "content_preview": "",
            "kind": kind,
            "editable": editable,
            "reason": None if editable else "memory file does not exist",
        }

    try:
        text = Path(resolved).read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()[:20]
        content_preview = "\n".join(lines)
        if len(text.splitlines()) > 20:
            content_preview += "\n... (truncated)"
    except OSError:
        pass

    return {
        "path": resolved,
        "exists": exists,
        "content_preview": content_preview,
        "kind": kind,
        "editable": True,
    }


async def handle_memory_status(
    workspace: str,
    mode: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    detailed = params.get("detailed", False)
    config = get_config()

    # code 模式现在也有 memory.enabled 配置项，直接读取
    enabled = is_memory_enabled(mode, config)
    proactive = is_proactive_memory(mode, config)

    result: dict[str, Any] = {
        "current_mode": mode,
        "storage_mode": config.get("memory", {}).get("mode", "local") if isinstance(config, dict) else "local",
        "engine": get_memory_engine(config),
        "enabled": enabled,
        "proactive": proactive,
        "forbidden_enabled": _is_forbidden_enabled(config),
        # auto_memory_enabled: agent mode 读全局（保留兼容）；code mode 读 auto_coding_memory
        "auto_memory_enabled": is_auto_memory_enabled(mode, config),
        # auto_coding_memory: 仅 code mode 有意义；agent mode 返回 None（UI 不展示）
        "auto_coding_memory": is_auto_memory_enabled(mode, config) if _is_code_mode(mode) else None,
    }

    if detailed:
        manager = await get_memory_manager(agent_id="default", workspace_dir=workspace)
        if manager is not None:
            status = manager.status()
            result["index"] = {
                "available": status.get("available", False),
                "provider": status.get("provider"),
                "model": status.get("model"),
                "files_count": status.get("files", 0),
                "chunks_count": status.get("chunks", 0),
                "dirty": status.get("dirty", False),
                "fts": status.get("fts", {}),
                "vector": status.get("vector", {}),
                "cache": status.get("cache", {}),
            }

        project_dir = params.get("project_dir")
        discover_workspace = project_dir or workspace
        clear_project_memory_cache(discover_workspace)
        project_files = discover_and_load_memory_files(
            workspace=discover_workspace,
            target_path=discover_workspace,
        )
        total_chars = sum(len(f.content) for f in project_files)
        large_files = get_large_memory_files(project_files)
        logger.info(
            "[memory_rpc] memory.status detailed: workspace=%s project_dir=%s "
            "discover_workspace=%s files=%d large_files=%d",
            workspace, project_dir, discover_workspace,
            len(project_files), len(large_files),
        )
        result["project_memory"] = {
            "files_count": len(project_files),
            "total_chars": total_chars,
            "max_chars": 60_000,
            "threshold": 40_000,
        }
        result["large_files"] = large_files
        if project_dir:
            result["project_memory"]["project_dir"] = project_dir

        coding_dir = _get_coding_memory_dir(workspace, project_dir)
        coding_files = _scan_md_files(coding_dir, "coding", workspace)
        coding_total_chars = 0
        for cf in coding_files:
            try:
                text = Path(cf["path"]).read_text(encoding="utf-8", errors="replace")
                coding_total_chars += len(text)
            except OSError:
                pass
        result["coding_memory"] = {
            "files_count": len(coding_files),
            "total_chars": coding_total_chars,
            "dir": coding_dir if os.path.isdir(coding_dir) else "",
        }

        # Auto-memory: unified with coding memory (same directory)
        if project_dir:
            # Auto-memory now uses coding memory directory (unified)
            result["auto_memory"] = {
                "files_count": len(coding_files),
                "total_chars": coding_total_chars,
                "dir": coding_dir if os.path.isdir(coding_dir) else "",
            }
        else:
            result["auto_memory"] = {
                "files_count": 0,
                "total_chars": 0,
                "dir": "",
            }

        engine = get_memory_engine(config)
        if engine in ("external", "both"):
            ext_cfg = get_external_memory_config(config)
            result["external_memory"] = {
                "provider": ext_cfg.get("provider", "unknown") if ext_cfg else "unknown",
                "enabled": is_external_memory_allowed(config),
            }

    return result


async def handle_memory_toggle(
    workspace: str,
    mode: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    from jiuwenswarm.common.config import update_memory_forbidden_enabled_in_config

    key = params.get("key", "")
    config = get_config()

    if key == "memory_forbidden_enabled":
        old = _is_forbidden_enabled(config)
        new = not old
        update_memory_forbidden_enabled_in_config(new)
        return {
            "key": key,
            "old_value": old,
            "new_value": new,
            "mode_affected": "global",
            "needs_restart": True,
        }

    if key in ("memory_enabled", "memory_proactive") and not (_is_code_mode(mode) or _is_agent_mode(mode)):
        old = is_memory_enabled(mode, config) if key == "memory_enabled" else is_proactive_memory(mode, config)
        return {
            "key": key,
            "old_value": old,
            "new_value": old,
            "mode_affected": mode,
            "needs_restart": False,
            "unsupported": True,
        }

    if key == "memory_proactive" and _is_agent_mode(mode):
        # agent 模式记忆恒被动（is_proactive_memory 对 agent 恒返回 False），
        # 该开关对 agent 无效，直接报 unsupported，避免"写入成功但从不生效"。
        old = is_proactive_memory(mode, config)
        return {
            "key": key,
            "old_value": old,
            "new_value": old,
            "mode_affected": mode,
            "needs_restart": False,
            "unsupported": True,
        }

    if key == "memory_enabled":
        old = is_memory_enabled(mode, config)
        new = not old
        _update_mode_memory_config(mode, "enabled", new)
        return {
            "key": key,
            "old_value": old,
            "new_value": new,
            "mode_affected": mode,
            "needs_restart": True,
        }

    if key == "memory_proactive":
        old = is_proactive_memory(mode, config)
        new = not old
        _update_mode_memory_config(mode, "is_proactive", new)
        return {
            "key": key,
            "old_value": old,
            "new_value": new,
            "mode_affected": mode,
            "needs_restart": True,
        }

    if key == "auto_coding_memory":
        # code mode 专属：子 agent 每轮兜底提取开关，写入 modes.code.memory.auto_coding_memory
        if not _is_code_mode(mode):
            return {
                "key": key,
                "old_value": False,
                "new_value": False,
                "mode_affected": "",
                "needs_restart": False,
                "error": f"auto_coding_memory is only valid in code mode (current: {mode})",
            }
        old = is_auto_memory_enabled(mode, config)
        new = not old
        _update_mode_memory_config(mode, "auto_coding_memory", new)
        logger.info(
            "[memory_rpc] Toggle auto_coding_memory: old=%s -> new=%s (mode=%s)",
            old, new, mode,
        )
        return {
            "key": key,
            "old_value": old,
            "new_value": new,
            "mode_affected": mode,
            "needs_restart": True,
        }

    if key == "auto_memory_enabled":
        # legacy 全局开关（agent mode 对话后提取）；UI 不暴露，保留兼容
        from jiuwenswarm.common.config import set_auto_memory_enabled
        old = bool(config.get("auto_memory_enabled", False))
        new = not old
        set_auto_memory_enabled(new)
        logger.info(
            "[memory_rpc] Toggle auto_memory_enabled: old=%s -> new=%s",
            old, new,
        )
        return {
            "key": key,
            "old_value": old,
            "new_value": new,
            "mode_affected": "global",
            "needs_restart": False,  # No restart needed, config read each session
        }

    return {
        "key": key,
        "old_value": False,
        "new_value": False,
        "mode_affected": "",
        "needs_restart": False,
    }


def _update_mode_memory_config(mode: str, field: str, value: bool) -> None:
    from jiuwenswarm.common.config import _load_yaml_round_trip, _dump_yaml_round_trip, _CONFIG_YAML_PATH

    if _is_code_mode(mode):
        target = "code"
    elif _is_agent_mode(mode):
        target = "agent"
    else:
        logger.warning(
            "[memory_rpc] _update_mode_memory_config: mode=%r 没有独立的记忆配置节点，忽略写入 field=%s",
            mode, field,
        )
        return

    data = _load_yaml_round_trip(_CONFIG_YAML_PATH)
    modes = data.setdefault("modes", {})
    node = modes.setdefault(target, {})
    memory = node.setdefault("memory", {})
    memory[field] = value
    _dump_yaml_round_trip(_CONFIG_YAML_PATH, data)


async def handle_memory_open(
    workspace: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    project_dir = params.get("project_dir")
    result: dict[str, Any] = {
        "memory_dir": os.path.join(workspace, "memory"),
        "project_memory_dir": workspace,
    }
    if project_dir:
        result["project_dir"] = project_dir
        # Unified: Auto-memory now uses coding memory directory
        coding_dir = _get_coding_memory_dir(workspace, project_dir)
        result["auto_memory_dir"] = coding_dir  # Backward compatibility
        result["coding_memory_dir"] = coding_dir
        logger.info(
            "[memory_rpc] memory.open: project_dir=%s unified_memory_dir=%s",
            project_dir, coding_dir,
        )
    else:
        logger.info("[memory_rpc] memory.open: no project_dir provided")
    return result
