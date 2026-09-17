# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""TUI-only HarmonyOS project inspection and persisted Agent context.

The persisted file is JiuwenSwarm-owned runtime state.  Project files are read
only and every path derived from ``build-profile.json5`` is confined to the
selected project root.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from json_repair import loads as json_repair_loads

from jiuwenswarm.common.utils import get_user_workspace_dir


SCHEMA_VERSION = 1
MAX_DESCRIPTOR_BYTES = 2 * 1024 * 1024
_PROJECT_MARKERS = ("build-profile.json5", "oh-package.json5")
_SAFE_ID_RE = re.compile(r"[^a-zA-Z0-9_-]+")


class HarmonyOSProjectError(ValueError):
    """Raised when a path is not a safe, inspectable HarmonyOS project."""


def inspect_harmonyos_project(project_path: str | os.PathLike[str]) -> dict[str, Any]:
    """Inspect HarmonyOS descriptors without modifying the project."""
    root = _resolve_project_root(project_path)
    warnings: list[str] = []
    ambiguities: list[str] = []
    source_files: list[str] = []
    descriptor_snapshots: dict[str, bytes | None] = {}
    watched_files: list[str] = [
        "build-profile.json5",
        "oh-package.json5",
        "AppScope/app.json5",
    ]

    build_profile = _read_optional_descriptor(
        root, "build-profile.json5", source_files, descriptor_snapshots
    )
    oh_package = _read_optional_descriptor(
        root, "oh-package.json5", source_files, descriptor_snapshots
    )
    if build_profile is None and oh_package is None:
        markers = ", ".join(_PROJECT_MARKERS)
        raise HarmonyOSProjectError(
            f"not a HarmonyOS project: expected {markers} under {root}"
        )
    if build_profile is None:
        warnings.append(
            "build-profile.json5 is missing; products and modules cannot be discovered"
        )
        build_profile = {}
    if oh_package is None:
        warnings.append("oh-package.json5 is missing; package metadata is unavailable")
        oh_package = {}

    app_scope = _read_optional_descriptor(
        root, "AppScope/app.json5", source_files, descriptor_snapshots
    )
    if app_scope is None:
        warnings.append("AppScope/app.json5 is missing; bundleName is unavailable")
        app_scope = {}

    app_profile = _as_dict(build_profile.get("app"))
    products = _extract_named_entries(app_profile.get("products"), "product", warnings)
    product_names = [item["name"] for item in products]
    default_product = _select_default(product_names, "default", "product", ambiguities)

    build_modes = _extract_named_entries(
        app_profile.get("buildModeSet") or build_profile.get("buildModeSet"),
        "build mode",
        warnings,
    )
    build_mode_names = [item["name"] for item in build_modes]
    if not build_mode_names:
        build_mode_names = ["debug"]
        warnings.append(
            "no buildModeSet found; using devecocli default build mode 'debug'"
        )

    modules: list[dict[str, Any]] = []
    raw_modules = build_profile.get("modules")
    if raw_modules is None:
        raw_modules = []
    if not isinstance(raw_modules, list):
        raise HarmonyOSProjectError(
            "build-profile.json5 field 'modules' must be an array"
        )

    for index, raw_module in enumerate(raw_modules):
        if not isinstance(raw_module, dict):
            warnings.append(f"modules[{index}] is not an object and was ignored")
            continue
        name = _clean_text(raw_module.get("name"))
        src_path = _clean_text(raw_module.get("srcPath"))
        if not name or not src_path:
            warnings.append(
                f"modules[{index}] is missing name or srcPath and was ignored"
            )
            continue

        module_dir = _resolve_project_child(root, src_path, expect_dir=True)
        descriptor_path = module_dir / "src" / "main" / "module.json5"
        watched_files.append(_relative_posix(root, descriptor_path))
        descriptor = _read_descriptor_at(
            root,
            descriptor_path,
            source_files,
            descriptor_snapshots,
            required=False,
        )
        descriptor_found = descriptor is not None
        if descriptor is None:
            warnings.append(f"module {name!r} has no src/main/module.json5")
            descriptor = {}
        module_profile = _as_dict(descriptor.get("module"))
        declared_name = _clean_text(module_profile.get("name"))
        if declared_name and declared_name != name:
            warnings.append(
                f"module {name!r} descriptor declares name {declared_name!r}"
            )

        targets = _extract_module_targets(raw_module.get("targets"), name, warnings)
        abilities = _extract_abilities(module_profile.get("abilities"), name, warnings)
        main_element = _clean_text(module_profile.get("mainElement"))
        selected_ability = _select_ability(name, abilities, main_element, ambiguities)
        module_type = _clean_text(module_profile.get("type"))
        modules.append(
            {
                "name": name,
                "type": module_type,
                "srcPath": _relative_posix(root, module_dir),
                "targets": targets,
                "mainElement": main_element,
                "abilities": abilities,
                "selectedAbility": selected_ability,
                "descriptor": (
                    _relative_posix(root, descriptor_path.resolve())
                    if descriptor_found
                    else None
                ),
            }
        )

    selected_module = _select_module(modules, ambiguities)
    selected_ability = None
    if selected_module:
        for module in modules:
            if module["name"] == selected_module:
                selected_ability = module.get("selectedAbility")
                break

    app_metadata = _as_dict(app_scope.get("app"))
    bundle_name = _clean_text(app_metadata.get("bundleName"))
    package_name = _clean_text(oh_package.get("name"))
    app_label = _clean_text(app_metadata.get("label"))
    if app_label and app_label.startswith("$"):
        app_label = None
    project_name = package_name or app_label or root.name
    project_id = _project_id(root, project_name)

    if not modules:
        warnings.append("no valid modules were discovered")
    if not product_names:
        warnings.append("no products were discovered")

    normalized_watched_files = sorted(set(watched_files))
    source_fingerprint = _fingerprint_descriptor_snapshots(
        {
            relative_path: descriptor_snapshots[relative_path]
            for relative_path in normalized_watched_files
        }
    )

    return {
        "schemaVersion": SCHEMA_VERSION,
        "kind": "harmonyos-project",
        "project": {
            "id": project_id,
            "name": project_name,
            "path": str(root),
            "packageName": package_name,
            "bundleName": bundle_name,
        },
        "products": products,
        "defaultProduct": default_product,
        "buildModes": build_mode_names,
        "modules": modules,
        "selected": {
            "product": default_product,
            "module": selected_module,
            "ability": selected_ability,
        },
        "ambiguities": ambiguities,
        "warnings": warnings,
        "sourceFiles": sorted(set(source_files)),
        "watchedFiles": normalized_watched_files,
        "sourceFingerprint": source_fingerprint,
        "updatedAt": _utc_now(),
    }


def persist_harmonyos_project_context(context: dict[str, Any]) -> Path:
    """Atomically persist validated HarmonyOS context outside the project."""
    if context.get("stale") is True:
        raise HarmonyOSProjectError(
            "refusing to persist stale HarmonyOS project context"
        )
    project = _as_dict(context.get("project"))
    project_path = project.get("path")
    if not isinstance(project_path, str) or not project_path.strip():
        raise HarmonyOSProjectError("context.project.path is required")
    root = _resolve_project_root(project_path)
    project_id = _project_id(root, _clean_text(project.get("name")) or root.name)

    payload = deepcopy(context)
    payload.pop("stale", None)
    payload.pop("refreshError", None)
    payload["schemaVersion"] = SCHEMA_VERSION
    payload["kind"] = "harmonyos-project"
    payload.setdefault("project", {})["id"] = project_id
    payload["project"]["path"] = str(root)
    payload["updatedAt"] = _utc_now()

    state_dir = _state_dir()
    _ensure_state_dir(state_dir)
    target = state_dir / f"{project_id}.json"
    if target.is_symlink():
        raise HarmonyOSProjectError(f"refusing symbolic-link state file: {target}")

    serialized = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{project_id}.", suffix=".tmp", dir=state_dir
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, target)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return target


def load_harmonyos_project_context(
    project_path: str | os.PathLike[str],
    *,
    allow_stale: bool = False,
) -> dict[str, Any] | None:
    """Load state only when its canonical project path matches the request.

    Control paths (MCP status, init) must keep the default ``allow_stale=False``
    fail-closed behavior. Prompt injection may pass ``allow_stale=True`` so a
    temporary descriptor read failure still reuses the last known snapshot
    instead of dropping engineering context entirely. Stale fallbacks are deep
    copies marked with ``stale`` / ``refreshError`` and are never re-persisted.
    """
    try:
        root = _resolve_project_root(project_path)
    except HarmonyOSProjectError:
        return None
    state_dir = _state_dir()
    if state_dir.is_symlink() or not state_dir.is_dir():
        return None

    # Project ids include the canonical path hash, so a glob avoids depending
    # on a project name that may have changed since initialization.
    path_hash = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]

    def state_mtime(item: Path) -> float:
        try:
            return item.stat().st_mtime if not item.is_symlink() else -1
        except OSError:
            return -1

    matches = sorted(
        state_dir.glob(f"*-{path_hash}.json"), key=state_mtime, reverse=True
    )
    for match in matches:
        if match.is_symlink():
            continue
        try:
            payload = json.loads(match.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        project = payload.get("project")
        if not isinstance(project, dict) or project.get("path") != str(root):
            continue
        if payload.get("schemaVersion") != SCHEMA_VERSION:
            continue
        if not _context_is_fresh(root, payload):
            try:
                refreshed = inspect_harmonyos_project(root)
            except HarmonyOSProjectError as exc:
                if not allow_stale:
                    return None
                stale = deepcopy(payload)
                stale["stale"] = True
                stale["refreshError"] = _clip_text(str(exc), 2000)
                return stale
            try:
                persist_harmonyos_project_context(refreshed)
            except (HarmonyOSProjectError, OSError):
                pass
            return refreshed
        return payload
    return None


def _resolve_project_root(project_path: str | os.PathLike[str]) -> Path:
    raw = str(project_path).strip()
    if not raw:
        raise HarmonyOSProjectError("project path is required")
    try:
        root = Path(raw).expanduser().resolve(strict=True)
    except OSError as exc:
        raise HarmonyOSProjectError(f"project path does not exist: {raw}") from exc
    if not root.is_dir():
        raise HarmonyOSProjectError(f"project path is not a directory: {root}")
    return root


def _read_optional_descriptor(
    root: Path,
    relative_path: str,
    source_files: list[str],
    descriptor_snapshots: dict[str, bytes | None],
) -> dict[str, Any] | None:
    return _read_descriptor_at(
        root,
        root / relative_path,
        source_files,
        descriptor_snapshots,
        required=False,
    )


def _read_descriptor_at(
    root: Path,
    path: Path,
    source_files: list[str],
    descriptor_snapshots: dict[str, bytes | None],
    *,
    required: bool,
) -> dict[str, Any] | None:
    try:
        relative_path = _relative_posix(root, path)
    except ValueError as exc:
        raise HarmonyOSProjectError(f"descriptor escapes project root: {path}") from exc
    resolved, raw_bytes = _safe_read_project_file_bytes(
        root, path, missing_ok=not required, error_verb="read"
    )
    if raw_bytes is None:
        descriptor_snapshots[relative_path] = None
        return None
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HarmonyOSProjectError(
            f"cannot decode descriptor {resolved}: {exc}"
        ) from exc
    # Reject mid-write / truncated files before json_repair can invent closing
    # quotes and braces. Legitimate JSON5 (unquoted keys, single quotes,
    # trailing commas, comments) remains accepted.
    _assert_json5_structurally_complete(text, resolved)
    try:
        parsed = json_repair_loads(text)
    except ValueError as exc:
        raise HarmonyOSProjectError(
            f"cannot parse JSON5 descriptor {resolved}: {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise HarmonyOSProjectError(f"descriptor root must be an object: {resolved}")
    if resolved is None:
        raise HarmonyOSProjectError(f"descriptor path could not be resolved: {path}")
    descriptor_snapshots[relative_path] = raw_bytes
    source_files.append(_relative_posix(root, resolved))
    return parsed


def _resolve_project_child(root: Path, raw_path: str, *, expect_dir: bool) -> Path:
    candidate = root / raw_path
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise HarmonyOSProjectError(
            f"module srcPath escapes project root or does not exist: {raw_path!r}"
        ) from exc
    _assert_no_symlink_between(root, candidate)
    if expect_dir and not resolved.is_dir():
        raise HarmonyOSProjectError(f"module srcPath is not a directory: {raw_path!r}")
    return resolved


def _context_is_fresh(root: Path, payload: dict[str, Any]) -> bool:
    expected = payload.get("sourceFingerprint")
    watched_files = payload.get("watchedFiles")
    if not isinstance(expected, str) or not isinstance(watched_files, list):
        return False
    if not all(isinstance(item, str) and item for item in watched_files):
        return False
    try:
        current = _descriptor_fingerprint(root, watched_files)
    except HarmonyOSProjectError:
        return False
    return current == expected


def _assert_json5_structurally_complete(text: str, path: Path | str) -> None:
    """Reject truncated JSON5 that json_repair would otherwise silently close.

    This is intentionally narrower than "any repair happened": unquoted keys,
    single-quoted strings, trailing commas, and comments are valid JSON5 and
    must keep working. Only structural incompleteness is fatal:
    unclosed strings, braces/brackets, or block comments.
    """
    stack: list[str] = []
    in_string: str | None = None
    escaped = False
    in_line_comment = False
    in_block_comment = False
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        nxt = text[index + 1] if index + 1 < length else ""
        if in_line_comment:
            if char == "\n":
                in_line_comment = False
            index += 1
            continue
        if in_block_comment:
            if char == "*" and nxt == "/":
                in_block_comment = False
                index += 2
                continue
            index += 1
            continue
        if in_string is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == in_string:
                in_string = None
            index += 1
            continue
        if char in "\"'":
            in_string = char
            index += 1
            continue
        if char == "/" and nxt == "/":
            in_line_comment = True
            index += 2
            continue
        if char == "/" and nxt == "*":
            in_block_comment = True
            index += 2
            continue
        if char in "{[":
            stack.append(char)
            index += 1
            continue
        if char in "}]":
            if not stack:
                raise HarmonyOSProjectError(
                    f"descriptor is structurally incomplete (unmatched {char}): {path}"
                )
            opener = stack.pop()
            if (opener, char) not in (("{", "}"), ("[", "]")):
                raise HarmonyOSProjectError(
                    f"descriptor is structurally incomplete "
                    f"(mismatched {opener}{char}): {path}"
                )
            index += 1
            continue
        index += 1

    if in_string is not None:
        raise HarmonyOSProjectError(
            f"descriptor is structurally incomplete (truncated string): {path}"
        )
    if in_block_comment:
        raise HarmonyOSProjectError(
            f"descriptor is structurally incomplete (unclosed block comment): {path}"
        )
    if stack:
        raise HarmonyOSProjectError(
            f"descriptor is structurally incomplete (unclosed {stack[-1]}): {path}"
        )


def _safe_read_project_file_bytes(
    root: Path,
    candidate: Path,
    *,
    missing_ok: bool,
    error_verb: str = "read",
) -> tuple[Path | None, bytes | None]:
    """Read a non-symlink file confined to ``root`` with a hard size limit.

    Returns ``(None, None)`` only when the path is missing and ``missing_ok`` is
    true. On success returns ``(resolved_path, raw_bytes)``.
    """
    if candidate.is_symlink():
        raise HarmonyOSProjectError(
            f"descriptor must not be a symbolic link: {candidate}"
        )
    if not candidate.exists():
        if missing_ok:
            return None, None
        raise HarmonyOSProjectError(f"required descriptor is missing: {candidate}")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise HarmonyOSProjectError(
            f"descriptor escapes project root: {candidate}"
        ) from exc
    if not resolved.is_file():
        raise HarmonyOSProjectError(f"descriptor is not a file: {resolved}")
    try:
        with resolved.open("rb") as handle:
            raw_bytes = handle.read(MAX_DESCRIPTOR_BYTES + 1)
    except OSError as exc:
        raise HarmonyOSProjectError(
            f"cannot {error_verb} descriptor {resolved}: {exc}"
        ) from exc
    if len(raw_bytes) > MAX_DESCRIPTOR_BYTES:
        raise HarmonyOSProjectError(f"descriptor is too large: {resolved}")
    return resolved, raw_bytes


def _descriptor_fingerprint(root: Path, relative_paths: list[str]) -> str:
    descriptor_snapshots: dict[str, bytes | None] = {}
    for relative_path in sorted(set(relative_paths)):
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise HarmonyOSProjectError(
                f"invalid descriptor fingerprint path: {relative_path!r}"
            )
        candidate = root / relative
        normalized_relative_path = relative.as_posix()
        _resolved, raw_bytes = _safe_read_project_file_bytes(
            root, candidate, missing_ok=True, error_verb="fingerprint"
        )
        descriptor_snapshots[normalized_relative_path] = raw_bytes
    return _fingerprint_descriptor_snapshots(descriptor_snapshots)


def _fingerprint_descriptor_snapshots(
    descriptor_snapshots: dict[str, bytes | None],
) -> str:
    digest = hashlib.sha256()
    for relative_path in sorted(descriptor_snapshots):
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        raw_bytes = descriptor_snapshots[relative_path]
        if raw_bytes is None:
            digest.update(b"missing\0")
            continue
        digest.update(b"file\0")
        digest.update(raw_bytes)
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def _assert_no_symlink_between(root: Path, candidate: Path) -> None:
    try:
        relative = candidate.absolute().relative_to(root)
    except ValueError as exc:
        raise HarmonyOSProjectError(f"path escapes project root: {candidate}") from exc
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise HarmonyOSProjectError(
                f"symbolic links are not allowed in module srcPath: {candidate}"
            )


def _extract_named_entries(
    raw: Any, label: str, warnings: list[str]
) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        warnings.append(f"{label} list is not an array and was ignored")
        return []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            warnings.append(f"{label}[{index}] is not an object and was ignored")
            continue
        name = _clean_text(item.get("name"))
        if not name or name in seen:
            warnings.append(
                f"{label}[{index}] has a missing or duplicate name and was ignored"
            )
            continue
        seen.add(name)
        result.append({"name": name})
    return result


def _extract_module_targets(
    raw: Any, module_name: str, warnings: list[str]
) -> list[str]:
    entries = _extract_named_entries(raw, f"module {module_name} target", warnings)
    return [entry["name"] for entry in entries]


def _extract_abilities(
    raw: Any, module_name: str, warnings: list[str]
) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        warnings.append(f"module {module_name!r} abilities is not an array")
        return []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            warnings.append(f"module {module_name!r} ability[{index}] is not an object")
            continue
        name = _clean_text(item.get("name"))
        if not name or name in seen:
            warnings.append(
                f"module {module_name!r} ability[{index}] has a missing or duplicate name"
            )
            continue
        seen.add(name)
        result.append(
            {
                "name": name,
                "srcEntry": _clean_text(item.get("srcEntry")),
                "exported": bool(item.get("exported", False)),
            }
        )
    return result


def _select_default(
    names: list[str], preferred: str, label: str, ambiguities: list[str]
) -> str | None:
    if preferred in names:
        return preferred
    if len(names) == 1:
        return names[0]
    if len(names) > 1:
        ambiguities.append(
            f"multiple {label}s found; select one explicitly: {', '.join(names)}"
        )
    return None


def _select_module(modules: list[dict[str, Any]], ambiguities: list[str]) -> str | None:
    entry_modules = [item["name"] for item in modules if item.get("type") == "entry"]
    if len(entry_modules) == 1:
        return entry_modules[0]
    if len(entry_modules) > 1:
        ambiguities.append(
            f"multiple entry modules found; select one explicitly: {', '.join(entry_modules)}"
        )
        return None
    if len(modules) == 1:
        return modules[0]["name"]
    if len(modules) > 1:
        ambiguities.append(
            "no unique entry module found; select one explicitly: "
            + ", ".join(item["name"] for item in modules)
        )
    return None


def _select_ability(
    module_name: str,
    abilities: list[dict[str, Any]],
    main_element: str | None,
    ambiguities: list[str],
) -> str | None:
    names = [item["name"] for item in abilities]
    if main_element and main_element in names:
        return main_element
    if main_element and main_element not in names:
        ambiguities.append(
            f"module {module_name!r} mainElement {main_element!r} does not match an ability"
        )
    if len(names) == 1:
        return names[0]
    if len(names) > 1:
        ambiguities.append(
            f"module {module_name!r} has multiple abilities; select one explicitly: {', '.join(names)}"
        )
    return None


def _state_dir() -> Path:
    return get_user_workspace_dir() / "agent" / "workspace" / "harmonyos-projects"


def _ensure_state_dir(state_dir: Path) -> None:
    if state_dir.is_symlink():
        raise HarmonyOSProjectError(
            f"state directory must not be a symbolic link: {state_dir}"
        )
    state_dir.mkdir(parents=True, exist_ok=True)
    if not state_dir.is_dir():
        raise HarmonyOSProjectError(f"state path is not a directory: {state_dir}")


def _project_id(root: Path, name: str) -> str:
    slug = _SAFE_ID_RE.sub("-", name).strip("-").lower() or "harmonyos-project"
    slug = slug[:48]
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]
    return f"{slug}-{digest}"


def _relative_posix(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value else None


def _clip_text(value: Any, limit: int) -> str | None:
    cleaned = _clean_text(value)
    if cleaned is None:
        return None
    return cleaned if len(cleaned) <= limit else cleaned[:limit] + "...[truncated]"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "HarmonyOSProjectError",
    "inspect_harmonyos_project",
    "load_harmonyos_project_context",
    "persist_harmonyos_project_context",
]
