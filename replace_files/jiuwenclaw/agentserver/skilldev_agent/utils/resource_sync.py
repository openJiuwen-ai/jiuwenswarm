# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SkillDev resource synchronization helpers."""

from __future__ import annotations

import base64
import json
import logging
import shutil
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from jiuwenclaw.agentserver.skilldev.common_utils import safe_extract_zip
from jiuwenclaw.agentserver.skilldev.utils.download_file_from_url import download_file

logger = logging.getLogger(__name__)

STATE_FILENAME = "resource_state.json"
STATE_DIRECT_IMPORTED = "direct_imported_skills"
STATE_REF_FILES = "ref_files"
STATE_REF_SKILLS = "ref_skills"
STATE_TOOL_SPECS = "tool_specs"

_ARCHIVE_SUFFIXES = frozenset({".zip", ".skill"})
_DELETE_ITEM_TYPES = frozenset(
    {"file", "skill", "toolDefinition", "agentDefinition", "cliDefinition"}
)


def resource_state_path(task_workspace: str | Path) -> Path:
    return Path(task_workspace) / "resources" / STATE_FILENAME


def load_resource_state(task_workspace: str | Path) -> dict[str, Any]:
    """Load resource sync state, falling back to an empty state on errors."""
    path = resource_state_path(task_workspace)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("[resource_sync] failed to read %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        logger.warning("[resource_sync] ignore non-object state: %s", path)
        return {}
    return data


def save_resource_state(task_workspace: str | Path, state: dict[str, Any]) -> None:
    """Persist resource sync state without interrupting the caller on failure."""
    path = resource_state_path(task_workspace)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("[resource_sync] failed to write %s: %s", path, exc)


def record_direct_imported_skills(task_workspace: str | Path, packages: list[dict[str, Any]]) -> None:
    """Record directImport package filenames so reference sync can skip them."""
    package_names = {
        _resource_filename(item)
        for item in packages
        if isinstance(item, dict) and _resource_filename(item)
    }
    if not package_names:
        return
    state = load_resource_state(task_workspace)
    existing = {
        str(item)
        for item in state.get(STATE_DIRECT_IMPORTED, [])
        if str(item).strip()
    }
    state[STATE_DIRECT_IMPORTED] = sorted(existing | package_names)
    save_resource_state(task_workspace, state)


def delete_uploaded_resources(
    task_workspace: str | Path,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Delete previously uploaded resources by identity.

    Returns ``{ok, deleted, notFound, errors}`` matching the Channel contract.
    ``notFound`` does not make ``ok`` false; only entries in ``errors`` do.
    """
    task_workspace = Path(task_workspace)
    deleted: list[dict[str, Any]] = []
    not_found: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    state = load_resource_state(task_workspace)
    state_dirty = False

    for raw in items:
        if not isinstance(raw, dict):
            errors.append({"type": "unknown", "error": "item must be an object"})
            continue
        item_type = str(raw.get("type") or "").strip()
        identity = _delete_item_identity(raw, item_type)
        try:
            result = _delete_one_resource(task_workspace, identity, state)
        except Exception as exc:
            logger.warning("[resource_sync] delete failed for %s: %s", identity, exc)
            errors.append({**identity, "error": str(exc)})
            continue
        if result == "deleted":
            deleted.append(identity)
            state_dirty = True
        elif result == "notFound":
            not_found.append(identity)
        else:
            errors.append({**identity, "error": result})

    if state_dirty:
        save_resource_state(task_workspace, state)

    return {
        "ok": len(errors) == 0,
        "deleted": deleted,
        "notFound": not_found,
        "errors": errors,
    }


def _delete_item_identity(raw: dict[str, Any], item_type: str) -> dict[str, Any]:
    """Normalize identity fields echoed back in delete responses."""
    if item_type not in _DELETE_ITEM_TYPES:
        return {"type": item_type or "unknown"}
    item: dict[str, Any] = {"type": item_type}
    if item_type in {"file", "skill"}:
        filename = str(raw.get("filename") or raw.get("name") or "").strip()
        if filename:
            item["filename"] = filename
    elif item_type == "toolDefinition":
        plugin_id = str(
            raw.get("pluginId") or raw.get("bundleName") or raw.get("plugin_id") or ""
        ).strip()
        tool_name = str(raw.get("toolName") or raw.get("name") or "").strip()
        if plugin_id:
            item["pluginId"] = plugin_id
        if tool_name:
            item["toolName"] = tool_name
    elif item_type == "agentDefinition":
        agent_id = str(raw.get("agentId") or raw.get("agent_id") or "").strip()
        if agent_id:
            item["agentId"] = agent_id
    else:  # cliDefinition
        name = str(raw.get("name") or "").strip()
        if name:
            item["name"] = name
    return item


def _delete_one_resource(
    task_workspace: Path,
    identity: dict[str, Any],
    state: dict[str, Any],
) -> str:
    """Delete one resource. Returns ``deleted``, ``notFound``, or an error message."""
    item_type = str(identity.get("type") or "")
    if item_type not in _DELETE_ITEM_TYPES:
        return f"unsupported type: {item_type or 'unknown'}"

    if item_type in {"file", "skill"}:
        filename = str(identity.get("filename") or "").strip()
        if not filename:
            return "requires filename"
        if (
            "/" in filename
            or "\\" in filename
            or filename in {".", ".."}
            or ".." in filename
        ):
            return "filename is illegal"
        dest_dir = task_workspace / "resources" / (
            "ref-files" if item_type == "file" else "ref-skills"
        )
        state_key = STATE_REF_FILES if item_type == "file" else STATE_REF_SKILLS
        return _delete_file_or_skill(dest_dir, filename, state, state_key)

    if item_type == "toolDefinition":
        plugin_id = str(identity.get("pluginId") or "").strip()
        tool_name = str(identity.get("toolName") or "").strip()
        if not plugin_id or not tool_name:
            return "requires pluginId and toolName"
        return _delete_tool_definition(task_workspace, plugin_id, tool_name, state)

    if item_type == "agentDefinition":
        agent_id = str(identity.get("agentId") or "").strip()
        if not agent_id:
            return "requires agentId"
        path = task_workspace / "resources" / "agents" / "available_agents.json"
        return _delete_json_array_entry(
            path,
            match_key=lambda entry: str(
                entry.get("agentId") or entry.get("agent_id") or ""
            ).strip()
            == agent_id,
        )

    # cliDefinition
    name = str(identity.get("name") or "").strip()
    if not name:
        return "requires name"
    path = task_workspace / "resources" / "clis" / "available_clis.json"
    return _delete_json_array_entry(
        path,
        match_key=lambda entry: str(entry.get("name") or "").strip() == name,
    )


def _delete_file_or_skill(
    dest_dir: Path,
    filename: str,
    state: dict[str, Any],
    state_key: str,
) -> str:
    archive_path = _safe_child_path(dest_dir, filename)
    if archive_path is None:
        return "filename is illegal"

    suffix = Path(filename).suffix.lower()
    removed_anything = False

    if suffix in _ARCHIVE_SUFFIXES:
        stem_dir = _safe_child_path(dest_dir, Path(filename).stem)
        if stem_dir is not None and stem_dir.is_dir():
            shutil.rmtree(stem_dir)
            removed_anything = True

        if archive_path.is_file():
            _delete_zip_members_from_dest(archive_path, dest_dir)
            archive_path.unlink(missing_ok=True)
            removed_anything = True
    else:
        if archive_path.is_file():
            archive_path.unlink()
            removed_anything = True
        elif archive_path.is_dir():
            shutil.rmtree(archive_path)
            removed_anything = True

    entries = _state_entries(state.get(state_key, []))
    remaining = [e for e in entries if e.get("filename") != filename]
    if len(remaining) != len(entries):
        state[state_key] = _sorted_entries(remaining)
        removed_anything = True

    return "deleted" if removed_anything else "notFound"


def _delete_zip_members_from_dest(archive_path: Path, dest_dir: Path) -> None:
    """Remove members extracted into ``dest_dir`` (URL flat-extract layout)."""
    dest_resolved = dest_dir.resolve()
    try:
        with zipfile.ZipFile(archive_path, "r") as zf:
            member_names = zf.namelist()
    except Exception as exc:
        logger.warning(
            "[resource_sync] failed to list zip members %s: %s", archive_path, exc
        )
        return

    # Delete deepest paths first so files go before their parent dirs.
    for name in sorted(member_names, key=lambda n: n.count("/"), reverse=True):
        if not name or name.startswith("__MACOSX/") or "/__MACOSX/" in name:
            continue
        rel = name.rstrip("/")
        if not rel:
            continue
        target = dest_dir / rel
        try:
            target.resolve().relative_to(dest_resolved)
        except ValueError:
            continue
        if target.is_file():
            target.unlink(missing_ok=True)
        elif target.is_dir():
            shutil.rmtree(target, ignore_errors=True)

        # Best-effort cleanup of emptied parent directories (do not remove dest_dir).
        parent = target.parent
        while parent != dest_dir:
            try:
                parent.resolve().relative_to(dest_resolved)
            except ValueError:
                break
            if not parent.is_dir():
                break
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent


def _delete_tool_definition(
    task_workspace: Path,
    plugin_id: str,
    tool_name: str,
    state: dict[str, Any],
) -> str:
    from jiuwenclaw.agentserver.skilldev_agent.meta_tools.external_tool_registry import (
        tool_spec_filename,
    )

    filename = tool_spec_filename(plugin_id, tool_name)
    dest_dir = task_workspace / "resources" / "available-tools"
    json_file = _safe_child_path(dest_dir, filename)
    removed_anything = False

    if json_file is not None and json_file.is_file():
        json_file.unlink()
        removed_anything = True

    entries = _tool_state_entries(state.get(STATE_TOOL_SPECS, []))
    remaining = [
        e
        for e in entries
        if not (
            e.get("filename") == filename
            or (
                e.get("pluginId") == plugin_id
                and e.get("toolName") == tool_name
            )
        )
    ]
    if len(remaining) != len(entries):
        state[STATE_TOOL_SPECS] = sorted(
            remaining,
            key=lambda item: (item.get("pluginId", ""), item.get("toolName", "")),
        )
        removed_anything = True

    return "deleted" if removed_anything else "notFound"


def _delete_json_array_entry(
    path: Path,
    *,
    match_key: Callable[[dict[str, Any]], bool],
) -> str:
    if not path.is_file():
        return "notFound"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"failed to read {path.name}: {exc}") from exc

    if isinstance(data, dict):
        # Single-object file: treat as one-element array.
        entries = [data]
    elif isinstance(data, list):
        entries = data
    else:
        return "notFound"

    remaining: list[Any] = []
    matched = False
    for entry in entries:
        if isinstance(entry, dict) and match_key(entry):
            matched = True
            continue
        remaining.append(entry)

    if not matched:
        return "notFound"

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(remaining, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return "deleted"


async def write_uploaded_resources(
    task_workspace: Path,
    params: dict[str, Any],
) -> None:
    """Synchronize uploaded resources into the SkillDev workspace."""
    task_workspace = Path(task_workspace)
    state = load_resource_state(task_workspace)
    direct_imported = {
        str(item)
        for item in state.get(STATE_DIRECT_IMPORTED, [])
        if str(item).strip()
    }

    await _sync_upload_only_resource_group(
        _resource_list(params.get("files")),
        task_workspace / "resources" / "ref-files",
        state=state,
        state_key=STATE_REF_FILES,
        extract_zip_to_subdir=True,
        exclude_names=direct_imported,
    )
    await _sync_upload_only_resource_group(
        _resource_list(params.get("skill_packages") or params.get("skillPackages")),
        task_workspace / "resources" / "ref-skills",
        state=state,
        state_key=STATE_REF_SKILLS,
        extract_zip_to_subdir=True,
        allowed_suffixes=(".zip", ".skill"),
        exclude_names=direct_imported,
    )
    await _sync_tool_spec_files(
        _resource_list(params.get("tool_spec_files") or params.get("toolSpecFiles")),
        task_workspace / "resources" / "available-tools",
        state=state,
    )

    _write_agent_and_cli_definitions(task_workspace, params)
    save_resource_state(task_workspace, state)


async def _sync_upload_only_resource_group(
    resources: list[dict[str, Any]],
    dest_dir: Path,
    *,
    state: dict[str, Any],
    state_key: str,
    extract_zip_to_subdir: bool,
    allowed_suffixes: tuple[str, ...] | None = None,
    exclude_names: set[str] | None = None,
) -> None:
    """Sync ref-files/ref-skills for the current upload batch only (no diff deletion)."""
    if not resources:
        return

    exclude_names = exclude_names or set()
    current_items: list[tuple[dict[str, Any], dict[str, str]]] = []

    for res in resources:
        name = _resource_filename(res)
        if not name or name in exclude_names:
            continue
        suffix = Path(name).suffix.lower()
        if allowed_suffixes and suffix not in allowed_suffixes:
            raise ValueError(f"不支持的文件类型: {name}")
        if not _resource_has_payload(res):
            continue
        current_items.append((res, _resource_state_entry(res, name)))

    if not current_items:
        return

    pending_resources = [res for res, _ in current_items]
    await write_resource_group(
        pending_resources,
        dest_dir,
        extract_zip_to_subdir=extract_zip_to_subdir,
        allowed_suffixes=allowed_suffixes,
    )

    old_entries = _state_entries(state.get(state_key, []))
    current_entries = [entry for _, entry in current_items]
    state[state_key] = _sorted_entries(_merge_state_entries(old_entries, current_entries))


async def _sync_tool_spec_files(
    resources: list[dict[str, Any]],
    dest_dir: Path,
    *,
    state: dict[str, Any],
) -> None:
    from jiuwenclaw.agentserver.skilldev_agent.meta_tools.external_tool_registry import (
        resolve_tool_spec_identity,
        tool_spec_filename,
    )

    tool_defs = _collect_tool_definitions(resources)
    old_entries = _tool_state_entries(state.get(STATE_TOOL_SPECS, []))
    old_filenames = {
        entry["filename"]
        for entry in old_entries
        if entry.get("filename")
    }
    current_filenames: set[str] = set()
    current_state: list[dict[str, str]] = []
    pending_tool_defs: list[dict[str, Any]] = []

    for tool_def in tool_defs:
        plugin_id, tool_name = resolve_tool_spec_identity(tool_def)
        if not plugin_id or not tool_name:
            logger.warning("[resource_sync] skip tool_spec without identity: %s", tool_def)
            continue
        filename = tool_spec_filename(plugin_id, tool_name)
        current_filenames.add(filename)
        current_state.append(
            {"pluginId": plugin_id, "toolName": tool_name, "filename": filename}
        )
        if filename not in old_filenames:
            pending_tool_defs.append(tool_def)

    _delete_stale_tool_files(dest_dir, old_entries, current_filenames)

    if pending_tool_defs:
        await write_tool_spec_files(pending_tool_defs, dest_dir)

    state[STATE_TOOL_SPECS] = sorted(
        current_state,
        key=lambda item: (item.get("pluginId", ""), item.get("toolName", "")),
    )


def _resource_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _resource_filename(resource: dict[str, Any]) -> str:
    value = resource.get("filename") or resource.get("name") or ""
    name = str(value).strip()
    if name:
        return name
    path_value = str(resource.get("path") or "").strip()
    if path_value:
        return Path(path_value).name
    return ""


def _resource_source_url(resource: dict[str, Any]) -> str:
    for key in ("url", "uri", "innerurl", "innerUrl"):
        value = str(resource.get(key) or "").strip()
        if value:
            return value
    return ""


_IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".jfif"})


def _resource_mime_type(resource: dict[str, Any]) -> str:
    for key in ("mime", "mimeType", "type", "content_type", "contentType"):
        value = str(resource.get(key) or "").strip().lower()
        if value:
            return value
    return ""


def _is_image_resource(resource: dict[str, Any], filename: str) -> bool:
    mime = _resource_mime_type(resource)
    if mime.startswith("image/"):
        return True
    if mime in {"jpeg", "jpg", "png", "gif", "webp", "bmp", "svg", "svg+xml", "jfif"}:
        return True
    return Path(filename).suffix.lower() in _IMAGE_EXTENSIONS


def _format_ref_file_hint_line(name: str, archive_path: Path, resource: dict[str, Any]) -> str:
    line = f"- {name} -> 本地路径: {archive_path}"
    if _is_image_resource(resource, name):
        source_url = _resource_source_url(resource)
        if source_url:
            line = f"{line} ; 可下载url: {source_url}"
    return line


def _format_ref_file_removed_hint_line(name: str) -> str:
    return f"- {name} -> 已移除（用户本轮未再上传）"


def _resource_has_payload(resource: dict[str, Any]) -> bool:
    return bool(
        str(resource.get("url") or "").strip()
        or str(resource.get("base64Data") or resource.get("base64") or "").strip()
    )


def _current_ref_file_resources(
    files: Any,
    direct_imported: set[str],
) -> list[dict[str, Any]]:
    resources: list[dict[str, Any]] = []
    for res in _resource_list(files):
        name = _resource_filename(res)
        if not name or name in direct_imported:
            continue
        if not _resource_has_payload(res):
            continue
        resources.append(res)
    return resources


def _current_ref_skill_resources(
    skill_packages: Any,
    direct_imported: set[str],
) -> list[dict[str, Any]]:
    resources: list[dict[str, Any]] = []
    for res in _resource_list(skill_packages):
        name = _resource_filename(res)
        if not name or name in direct_imported:
            continue
        suffix = Path(name).suffix.lower()
        if suffix not in (".zip", ".skill"):
            raise ValueError(f"不支持的文件类型: {name}")
        if not _resource_has_payload(res):
            continue
        resources.append(res)
    return resources


def _ref_files_extract_to_stem_dir(resources: list[dict[str, Any]]) -> bool:
    """Mirror ``write_resource_group`` URL-batch detection for ref-files."""
    return not bool(resources and str(resources[0].get("url") or "").strip())


def _ref_file_extract_dir(ref_dir: Path, filename: str, *, extract_to_stem_dir: bool) -> Path:
    suffix = Path(filename).suffix.lower()
    if suffix in (".zip", ".skill"):
        return ref_dir / Path(filename).stem if extract_to_stem_dir else ref_dir
    return ref_dir / filename


def build_current_ref_file_hint_lines(
    task_workspace: str | Path,
    files: Any,
) -> list[str]:
    """Build hint lines for ref-files uploaded in the current round (no diff)."""
    task_workspace = Path(task_workspace)
    ref_dir = task_workspace / "resources" / "ref-files"
    state = load_resource_state(task_workspace)
    direct_imported = {
        str(item)
        for item in state.get(STATE_DIRECT_IMPORTED, [])
        if str(item).strip()
    }
    resources = _current_ref_file_resources(files, direct_imported)
    if not resources:
        return []

    extract_to_stem_dir = _ref_files_extract_to_stem_dir(resources)
    lines: list[str] = []
    for res in resources:
        name = _resource_filename(res)
        archive_path = (ref_dir / name).resolve()
        lines.append(_format_ref_file_hint_line(name, archive_path, res))
        suffix = Path(name).suffix.lower()
        if suffix in (".zip", ".skill"):
            extract_dir = _ref_file_extract_dir(
                ref_dir,
                name,
                extract_to_stem_dir=extract_to_stem_dir,
            ).resolve()
            lines.append(f"  - 解压目录 -> {extract_dir}")
    return lines


def build_current_ref_skill_hint_lines(
    task_workspace: str | Path,
    skill_packages: Any,
) -> list[str]:
    """Build hint lines for ref-skills uploaded in the current round (no diff)."""
    task_workspace = Path(task_workspace)
    ref_dir = task_workspace / "resources" / "ref-skills"
    state = load_resource_state(task_workspace)
    direct_imported = {
        str(item)
        for item in state.get(STATE_DIRECT_IMPORTED, [])
        if str(item).strip()
    }
    resources = _current_ref_skill_resources(skill_packages, direct_imported)
    if not resources:
        return []

    extract_to_stem_dir = _ref_files_extract_to_stem_dir(resources)
    lines: list[str] = []
    for res in resources:
        name = _resource_filename(res)
        archive_path = (ref_dir / name).resolve()
        lines.append(f"- {name} -> 本地路径: {archive_path}")
        extract_dir = _ref_file_extract_dir(
            ref_dir,
            name,
            extract_to_stem_dir=extract_to_stem_dir,
        ).resolve()
        lines.append(f"  - 解压目录 -> {extract_dir}")
    return lines


def build_current_tool_spec_hint_lines(
    task_workspace: str | Path,
    tool_spec_files: Any,
    *,
    previous_tool_specs: Any | None = None,
) -> tuple[list[str], list[str]]:
    """Build per-tool hint lines for tool specs uploaded in the current round.

    Returns a tuple of (added_lines, removed_lines) compared to the previous state.
    """
    task_workspace = Path(task_workspace)
    tools_dir = task_workspace / "resources" / "available-tools"

    from jiuwenclaw.agentserver.skilldev_agent.meta_tools.external_tool_registry import (
        resolve_tool_spec_identity,
        tool_spec_filename,
    )

    prev_entries = _tool_state_entries(previous_tool_specs or [])
    prev_filenames = {e.get("filename", "") for e in prev_entries if e.get("filename")}

    tool_defs = _collect_tool_definitions(_resource_list(tool_spec_files))
    current_entries: list[dict[str, str]] = []
    current_filenames: set[str] = set()
    for tool_def in tool_defs:
        plugin_id, tool_name = resolve_tool_spec_identity(tool_def)
        if not plugin_id or not tool_name:
            continue
        filename = tool_spec_filename(plugin_id, tool_name)
        current_filenames.add(filename)
        current_entries.append(
            {"pluginId": plugin_id, "toolName": tool_name, "filename": filename}
        )

    removed_lines: list[str] = []
    for entry in prev_entries:
        filename = entry.get("filename", "")
        if not filename or filename in current_filenames:
            continue
        plugin_id = entry.get("pluginId", "")
        tool_name = entry.get("toolName", "")
        if plugin_id and tool_name:
            removed_lines.append(
                f"- {plugin_id}/{tool_name} -> 已移除（用户本轮未再上传）"
            )
        else:
            removed_lines.append(_format_ref_file_removed_hint_line(filename))

    added_lines: list[str] = []
    for entry in current_entries:
        filename = entry.get("filename", "")
        if not filename or filename in prev_filenames:
            continue
        plugin_id = entry.get("pluginId", "")
        tool_name = entry.get("toolName", "")
        if not plugin_id or not tool_name:
            continue
        tool_path = (tools_dir / filename).resolve()
        added_lines.append(f"- {plugin_id}/{tool_name} -> 本地路径: {tool_path}")

    return (added_lines, removed_lines)


def _resource_state_entry(resource: dict[str, Any], name: str) -> dict[str, str]:
    entry = {"filename": name}
    url = str(resource.get("url") or "").strip()
    if url:
        entry["url"] = url
    return entry


def _state_entries(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    entries: list[dict[str, str]] = []
    for item in value:
        if isinstance(item, dict):
            filename = str(item.get("filename") or "").strip()
            if not filename:
                continue
            entry = {"filename": filename}
            url = str(item.get("url") or "").strip()
            if url:
                entry["url"] = url
            entries.append(entry)
        elif isinstance(item, str) and item.strip():
            entries.append({"filename": item.strip()})
    return entries


def _tool_state_entries(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    entries: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        filename = str(item.get("filename") or "").strip()
        if not filename:
            continue
        entry = {"filename": filename}
        plugin_id = str(item.get("pluginId") or "").strip()
        tool_name = str(item.get("toolName") or "").strip()
        if plugin_id:
            entry["pluginId"] = plugin_id
        if tool_name:
            entry["toolName"] = tool_name
        entries.append(entry)
    return entries


def _entry_key(entry: dict[str, str]) -> tuple[str, str]:
    return (entry.get("filename", ""), entry.get("url", ""))


def _sorted_entries(entries: list[dict[str, str]]) -> list[dict[str, str]]:
    return sorted(entries, key=lambda item: (item.get("filename", ""), item.get("url", "")))


def _merge_state_entries(
    old_entries: list[dict[str, str]],
    new_entries: list[dict[str, str]],
) -> list[dict[str, str]]:
    merged: dict[tuple[str, str], dict[str, str]] = {}
    for entry in old_entries + new_entries:
        merged[_entry_key(entry)] = entry
    return list(merged.values())


def _safe_child_path(dest_dir: Path, filename: str) -> Path | None:
    path = dest_dir / filename
    try:
        path.resolve().relative_to(dest_dir.resolve())
    except ValueError:
        logger.warning("[resource_sync] skip path outside resource dir: %s", filename)
        return None
    return path



def _delete_stale_tool_files(
    dest_dir: Path,
    old_entries: list[dict[str, str]],
    current_filenames: set[str],
) -> None:
    if not dest_dir.is_dir():
        return
    for entry in old_entries:
        filename = entry.get("filename", "")
        if not filename or filename in current_filenames:
            continue
        json_file = _safe_child_path(dest_dir, filename)
        if json_file is not None and json_file.is_file():
            json_file.unlink()


def _collect_tool_definitions(resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from jiuwenclaw.agentserver.skilldev_agent.meta_tools.external_tool_registry import (
        iter_tool_definitions_from_json,
        resolve_tool_spec_identity,
    )

    tool_defs: list[dict[str, Any]] = []
    for res in resources:
        content_b64 = str(res.get("base64Data") or res.get("base64") or "").strip()
        if content_b64:
            try:
                raw_bytes = base64.b64decode(content_b64)
                parsed = json.loads(raw_bytes.decode("utf-8"))
            except Exception as exc:
                fname = res.get("filename", "?")
                raise ValueError(f"工具定义文件 [{fname}] 解析失败: {exc}") from exc
            tool_defs.extend(iter_tool_definitions_from_json(parsed))
            continue

        plugin_id, tool_name = resolve_tool_spec_identity(res)
        if plugin_id and tool_name:
            tool_defs.append(res)
        else:
            logger.warning(
                "[resource_sync] skip tool_spec entry without base64 or identity: %s",
                res.get("filename", res),
            )
    return tool_defs


async def write_tool_spec_files(resources: list[dict[str, Any]], dest_dir: Path) -> None:
    """Write uploaded tool specs as ``<pluginId>__<toolName>.json`` (pass-through)."""
    from jiuwenclaw.agentserver.skilldev_agent.meta_tools.external_tool_registry import (
        iter_tool_definitions_from_json,
        resolve_tool_spec_identity,
        write_tool_spec_file,
    )

    if not resources:
        return
    dest_dir.mkdir(parents=True, exist_ok=True)
    for res in resources:
        content_b64 = res.get("base64Data") or res.get("base64") or ""
        if content_b64:
            try:
                raw_bytes = base64.b64decode(content_b64)
                parsed = json.loads(raw_bytes.decode("utf-8"))
            except Exception as exc:
                fname = res.get("filename", "?")
                raise ValueError(f"工具定义文件 [{fname}] 解析失败: {exc}") from exc
            for tool_def in iter_tool_definitions_from_json(parsed):
                write_tool_spec_file(dest_dir, tool_def)
        else:
            plugin_id, tool_name = resolve_tool_spec_identity(res)
            if plugin_id and tool_name:
                write_tool_spec_file(dest_dir, res)
            else:
                logger.warning(
                    "[SkillDevDeepAdapter] skip tool_spec entry without base64 or "
                    "pluginId/bundleName+toolName: %s",
                    res.get("filename", res),
                )


async def write_skill_searched(task_workspace: Path, skill_searched: dict[str, Any]) -> None:
    """Download a skill selected from search results into ref-skills."""
    skill_name = skill_searched.get("skillId") or skill_searched.get("skillName") or "unknown"
    url = skill_searched.get("url", "")
    if not url:
        logger.warning("[SkillDevDeepAdapter] skill_searched missing url: %s", skill_searched)
        return

    dest_dir = task_workspace / "resources" / "ref-skills"
    dest_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(url).suffix.lower() or ".skill"
    if ".skill" in suffix:
        suffix = ".skill"
    elif ".zip" in suffix:
        suffix = ".zip"
    file_path = dest_dir / f"{skill_name}{suffix}"
    await download_file(url, str(file_path))
    if suffix in (".zip", ".skill"):
        safe_extract_zip(file_path, dest_dir, extract_to_stem_dir=False)


async def write_resource_group(
    resources: list[dict[str, Any]],
    dest_dir: Path,
    *,
    extract_zip_to_subdir: bool,
    allowed_suffixes: tuple[str, ...] | None = None,
) -> None:
    if not resources:
        return
    dest_dir.mkdir(parents=True, exist_ok=True)

    if resources[0].get("url", ""):
        for res in resources:
            name = str(res.get("filename") or res.get("name") or "unknown")
            suffix = Path(name).suffix.lower()
            if allowed_suffixes and suffix not in allowed_suffixes:
                raise ValueError(f"不支持的文件类型: {name}")
            download_url = str(res.get("url", ""))
            if not download_url:
                continue
            file_path = dest_dir / name
            await download_file(download_url, str(file_path))
            if suffix in (".zip", ".skill"):
                safe_extract_zip(file_path, dest_dir, extract_to_stem_dir=False)
        return

    for res in resources:
        name = str(res.get("filename") or res.get("name") or "unknown")
        suffix = Path(name).suffix.lower()
        if allowed_suffixes and suffix not in allowed_suffixes:
            raise ValueError(f"不支持的文件类型: {name}")
        content_b64 = res.get("base64Data") or res.get("base64") or ""
        if not content_b64:
            continue
        file_path = dest_dir / name
        file_path.write_bytes(base64.b64decode(content_b64))
        if suffix in (".zip", ".skill"):
            safe_extract_zip(file_path, dest_dir, extract_to_stem_dir=extract_zip_to_subdir)


def _write_agent_and_cli_definitions(task_workspace: Path, params: dict[str, Any]) -> None:
    agent_definitions = params.get("agent_definitions") or params.get("agentDefinitions")
    if agent_definitions:
        agents_dir = task_workspace / "resources" / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        (agents_dir / "available_agents.json").write_text(
            json.dumps(agent_definitions, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    cli_definitions = params.get("cli_definitions") or params.get("cliDefinitions")
    if cli_definitions:
        clis_dir = task_workspace / "resources" / "clis"
        clis_dir.mkdir(parents=True, exist_ok=True)
        (clis_dir / "available_clis.json").write_text(
            json.dumps(cli_definitions, ensure_ascii=False, indent=2), encoding="utf-8"
        )
