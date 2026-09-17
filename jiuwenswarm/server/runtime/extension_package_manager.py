# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Agent_template / plugin package manager (catalog + lifecycle)."""

from __future__ import annotations

import json
import logging
import re
import shutil
from pathlib import Path
from pathlib import PureWindowsPath
from typing import Any

import yaml

from jiuwenswarm.common.utils import (
    get_agent_skills_dir,
    get_agent_workspace_dir,
)
from jiuwenswarm.server.runtime.mcp.state_store import get_mcp_record

logger = logging.getLogger(__name__)

_CATALOG_ROOT_PREVIEWABLE_FILES: frozenset[str] = frozenset({"README.md", "manifest.json"})
_CATALOG_PREVIEWABLE_DIRS: frozenset[str] = frozenset(
    {"persona", "skills", "tools", "rails", "subagents"}
)
_CATALOG_PREVIEWABLE_EXTS: frozenset[str] = frozenset({".md", ".py"})
_MAX_PREVIEW_FILE_BYTES = 1 * 1024 * 1024
_AGENT_TEMPLATE_KIND = "agent_templates"
_PLUGIN_PACKAGE_KIND = "plugin_packages"


def _reject_package_name(name: Any, kind: str) -> str:
    """Reject empty, dotted, separator, or absolute package names."""
    raw = str(name or "").strip()
    if not raw:
        raise ValueError(f"invalid {kind} name: empty")
    if raw in (".", ".."):
        raise ValueError(f"invalid {kind} name: {raw}")
    if "/" in raw or "\\" in raw:
        raise ValueError(f"invalid {kind} name (path separator): {raw}")
    if Path(raw).is_absolute() or PureWindowsPath(raw).is_absolute():
        raise ValueError(f"invalid {kind} name (absolute): {raw}")
    return raw


def _resources_plugins_root() -> Path | None:
    """Return package resources/.../plugins if present."""
    current = Path(__file__).resolve()
    for parent in current.parents:
        candidate = parent / "resources" / "agent" / "workspace" / "plugins"
        if candidate.is_dir():
            return candidate
    return None


def get_equipment_resources_agent_templates_dir() -> Path | None:
    """Return package resources agent_templates dir, or None if absent."""
    root = _resources_plugins_root()
    if root is None:
        return None
    path = root / _AGENT_TEMPLATE_KIND
    return path if path.is_dir() else None


def get_equipment_resources_plugin_packages_dir() -> Path | None:
    """Return package resources plugin_packages dir, or None if absent."""
    root = _resources_plugins_root()
    if root is None:
        return None
    path = root / _PLUGIN_PACKAGE_KIND
    return path if path.is_dir() else None


def _is_previewable_file(rel_posix: str) -> bool:
    """Return whether a package-relative path may be listed or read."""
    if not rel_posix:
        return False
    parts = rel_posix.split("/")
    if any(part.startswith(".") for part in parts):
        return False
    if len(parts) == 1:
        return parts[0] in _CATALOG_ROOT_PREVIEWABLE_FILES
    if parts[0] not in _CATALOG_PREVIEWABLE_DIRS:
        return False
    if len(parts) < 2:
        return False
    return Path(rel_posix).suffix in _CATALOG_PREVIEWABLE_EXTS


def _read_package_manifest(pkg_dir: Path) -> dict | None:
    """Parse package manifest.json, or None if missing/corrupt."""
    manifest_path = pkg_dir / "manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _source_from_pkg_dir(pkg_dir: Path) -> str:
    """Derive API source from ownership directory (built_in → builtin)."""
    parent = pkg_dir.parent.name
    if parent == "built_in":
        return "builtin"
    return "local"


def _i18n(value: Any, fallback: str = "") -> dict[str, str]:
    """Normalize a display string/dict into {zh, en}."""
    if isinstance(value, dict):
        zh = value.get("zh")
        en = value.get("en")
        zh_s = str(zh) if zh not in (None, "") else (str(en) if en not in (None, "") else fallback)
        en_s = str(en) if en not in (None, "") else (str(zh) if zh not in (None, "") else fallback)
        return {"zh": zh_s, "en": en_s}
    if isinstance(value, str) and value:
        return {"zh": value, "en": value}
    return {"zh": fallback, "en": fallback}


def _marketplace_index(entries: list[dict]) -> dict[str, dict]:
    """Index marketplace entries by id."""
    out: dict[str, dict] = {}
    for entry in entries:
        pkg_id = entry.get("id")
        if isinstance(pkg_id, str) and pkg_id:
            out[pkg_id] = entry
    return out


def _read_readme_details(pkg_dir: Path) -> str:
    """Return README.md text, or empty string."""
    readme = pkg_dir / "README.md"
    if not readme.is_file():
        return ""
    try:
        return readme.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _parse_skill_frontmatter(skill_md: Path) -> dict[str, Any]:
    """Parse SKILL.md YAML frontmatter for name/description (best-effort)."""
    try:
        text = skill_md.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?", text, re.DOTALL)
    if not match:
        return {}
    fm_text = match.group(1)
    try:
        loaded = yaml.safe_load(fm_text)
        if isinstance(loaded, dict):
            return loaded
    except Exception:
        logger.debug(
            "SKILL.md frontmatter YAML parse failed, falling back to line parser: %s",
            skill_md,
            exc_info=True,
        )
    meta: dict[str, Any] = {}
    for line in fm_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, val = line.partition(":")
        meta[key.strip()] = val.strip().strip("'\"")
    return meta


def _skill_id_from_spec(spec: Any) -> str | None:
    """Extract skill id (directory basename) from a manifest skills entry."""
    if isinstance(spec, str) and spec.strip():
        return Path(spec.strip()).name or None
    if isinstance(spec, dict):
        raw = spec.get("dir") or spec.get("id") or ""
        if isinstance(raw, str) and raw.strip():
            return Path(raw.strip()).name or None
    return None


def _map_skills(pkg_dir: Path, manifest: dict) -> list[dict]:
    """Map manifest skills → ability cards (id from dir name)."""
    specs = manifest.get("skills")
    if not isinstance(specs, list):
        return []
    cards: list[dict] = []
    for spec in specs:
        skill_id = _skill_id_from_spec(spec)
        if not skill_id:
            continue
        skill_md = pkg_dir / "skills" / skill_id / "SKILL.md"
        meta = _parse_skill_frontmatter(skill_md) if skill_md.is_file() else {}
        name = meta.get("name") if isinstance(meta.get("name"), str) else skill_id
        desc = meta.get("description")
        if not isinstance(desc, str):
            desc = ""
        cards.append(
            {
                "id": skill_id,
                "displayName": _i18n(name, skill_id),
                "displayDescription": _i18n(desc),
                "avatar": "",
            }
        )
    return cards


def _map_class_entries(manifest: dict, key: str) -> list[dict]:
    """Map tools/rails entries (class + display*) → ability cards."""
    specs = manifest.get(key)
    if not isinstance(specs, list):
        return []
    cards: list[dict] = []
    for spec in specs:
        if not isinstance(spec, dict):
            continue
        entry_id = spec.get("class") or spec.get("id")
        if not isinstance(entry_id, str) or not entry_id.strip():
            continue
        cards.append(
            {
                "id": entry_id.strip(),
                "displayName": _i18n(spec.get("displayName"), entry_id.strip()),
                "displayDescription": _i18n(spec.get("displayDescription")),
            }
        )
    return cards


def _connector_display(name: str) -> tuple[dict[str, str], dict[str, str]]:
    """Resolve connector marketplace displayName/description ({zh,en})."""
    try:
        from jiuwenswarm.server.runtime.mcp.registry import (
            get_connector_catalog_display,
        )

        return get_connector_catalog_display(name)
    except Exception:  # noqa: BLE001 — show must not fail if MCP catalog is unavailable
        logger.debug("connector display lookup failed for %s", name, exc_info=True)
        return _i18n(name, name), _i18n("")


def _map_mcps(pkg_dir: Path, manifest: dict) -> list[dict]:
    """Map mcps from manifest: package ``file``/``dir`` plus host ``connector`` deps."""
    cards: list[dict] = []
    seen: set[str] = set()

    def _add(
        mcp_id: str,
        display_name: Any = None,
        display_desc: Any = None,
        *,
        kind: str = "package",
    ) -> None:
        if not mcp_id or mcp_id in seen:
            return
        seen.add(mcp_id)
        cards.append(
            {
                "id": mcp_id,
                "displayName": _i18n(display_name, mcp_id),
                "displayDescription": _i18n(display_desc),
                "kind": kind,
            }
        )

    def _add_from_mcp_file(path: Path) -> None:
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return
        if not isinstance(data, dict):
            return
        mcp_servers = data.get("mcpServers")
        if isinstance(mcp_servers, dict):
            for name in mcp_servers:
                if isinstance(name, str) and name.strip():
                    _add(name.strip(), name.strip())
            return
        servers = data.get("servers")
        if isinstance(servers, list):
            for server in servers:
                if not isinstance(server, dict):
                    continue
                mcp_id = server.get("server_id") or server.get("name")
                if isinstance(mcp_id, str) and mcp_id.strip():
                    _add(mcp_id.strip(), mcp_id.strip(), server.get("description"))

    specs = manifest.get("mcps")
    if not isinstance(specs, list):
        return cards
    for spec in specs:
        if not isinstance(spec, dict):
            continue
        connector = spec.get("connector")
        if isinstance(connector, str) and connector.strip():
            name = connector.strip()
            dn, dd = _connector_display(name)
            if spec.get("displayName") not in (None, ""):
                dn = spec.get("displayName")
            if spec.get("displayDescription") not in (None, ""):
                dd = spec.get("displayDescription")
            _add(name, dn, dd, kind="connector")
            continue
        mcp_id = spec.get("id") or spec.get("server_id") or spec.get("name")
        if isinstance(mcp_id, str) and mcp_id.strip():
            _add(mcp_id.strip(), spec.get("displayName"), spec.get("displayDescription"))
            continue
        file_ref = spec.get("file")
        if isinstance(file_ref, str) and file_ref.strip():
            _add_from_mcp_file(pkg_dir / file_ref)
            continue
        dir_ref = spec.get("dir")
        if isinstance(dir_ref, str) and dir_ref.strip():
            mcp_dir = pkg_dir / dir_ref
            for filename in ("mcp.json", "mcps.json"):
                candidate = mcp_dir / filename
                if candidate.is_file():
                    _add_from_mcp_file(candidate)
                    break
    return cards


def _connector_names_from_manifest(manifest: dict) -> list[str]:
    """Deduplicate non-empty ``mcps[].connector`` names (same rules as install gate)."""
    specs = manifest.get("mcps")
    if specs is None:
        return []
    if not isinstance(specs, list):
        return []
    names: list[str] = []
    seen: set[str] = set()
    for spec in specs:
        if not isinstance(spec, dict) or "connector" not in spec:
            continue
        connector = spec.get("connector")
        if not isinstance(connector, str) or not connector.strip():
            continue
        name = connector.strip()
        if name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def _package_connection_state(manifest: dict, *, installed: bool) -> str:
    """Aggregate package-level MCP connection_state for list/show cards.

    No connectors → mirrors ``installed`` (connected if installed else disconnected).
    With connectors → AND over ``get_mcp_record`` states: any ``connecting`` wins;
    all ``connected`` → connected; otherwise disconnected.
    """
    names = _connector_names_from_manifest(manifest)
    if not names:
        return "connected" if installed else "disconnected"
    any_connecting = False
    all_connected = True
    for name in names:
        rec = get_mcp_record(name)
        state = rec.get("state") if isinstance(rec, dict) else None
        if state == "connecting":
            any_connecting = True
            all_connected = False
        elif state != "connected":
            all_connected = False
    if any_connecting:
        return "connecting"
    if all_connected:
        return "connected"
    return "disconnected"


def _build_list_card(
    pkg_dir: Path,
    *,
    package_type: str,
    source: str,
    marketplace: dict | None,
    installed: bool | None = None,
) -> dict | None:
    """Build one list card with marketplace status overlay."""
    manifest = _read_package_manifest(pkg_dir)
    if manifest is None or manifest.get("packageType") != package_type:
        return None
    category = manifest.get("category")
    card: dict[str, Any] = {
        "id": pkg_dir.name,
        "displayName": manifest.get("displayName") or pkg_dir.name,
        "displayDescription": manifest.get("displayDescription") or {},
        "category": category if isinstance(category, str) else "",
        "source": source,
    }
    if installed is not None:
        card["installed"] = installed
    elif marketplace is not None:
        card["installed"] = bool(marketplace.get("installed", False))
    else:
        card["installed"] = False
    card["connection_state"] = _package_connection_state(
        manifest, installed=bool(card["installed"])
    )
    return card


def _build_show_card(
    pkg_dir: Path,
    *,
    package_type: str,
    marketplace: dict | None,
    source: str | None = None,
    installed: bool | None = None,
) -> dict | None:
    """Build one show/detail card (no components; skills/tools/rails/mcps mapped)."""
    manifest = _read_package_manifest(pkg_dir)
    if manifest is None or manifest.get("packageType") != package_type:
        return None
    resolved_source = source if source is not None else _source_from_pkg_dir(pkg_dir)
    avatar = manifest.get("avatar")
    version = manifest.get("version")
    tags = manifest.get("tags")
    card: dict[str, Any] = {
        "id": pkg_dir.name,
        "displayName": manifest.get("displayName") or pkg_dir.name,
        "displayDescription": manifest.get("displayDescription") or {},
        "source": resolved_source,
        "avatar": avatar if isinstance(avatar, str) else "",
        "version": version if isinstance(version, str) else "",
        "details": _read_readme_details(pkg_dir),
        "tags": tags if isinstance(tags, list) else [],
        "skills": _map_skills(pkg_dir, manifest),
        "tools": _map_class_entries(manifest, "tools"),
        "rails": _map_class_entries(manifest, "rails"),
        "mcps": _map_mcps(pkg_dir, manifest),
    }
    if installed is not None:
        card["installed"] = installed
    elif marketplace is not None:
        card["installed"] = bool(marketplace.get("installed", False))
    else:
        card["installed"] = False
    card["connection_state"] = _package_connection_state(
        manifest, installed=bool(card["installed"])
    )
    # Same readiness rule as install gate: only state=="connected" is ready.
    card["pending_connectors"] = unready_connectors(
        _connector_names_from_manifest(manifest)
    )
    if package_type == "agent_template":
        quick = manifest.get("quickInputs")
        card["quickInputs"] = quick if isinstance(quick, list) else []
    return card


def _iter_resource_package_dirs(kind: str) -> list[Path]:
    """Return valid package dirs under the package resources shelf for kind."""
    if kind == "agent_templates":
        root = get_equipment_resources_agent_templates_dir()
    elif kind == "plugin_packages":
        root = get_equipment_resources_plugin_packages_dir()
    else:
        return []
    if root is None or not root.is_dir():
        return []
    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name)
    except OSError:
        return []
    out: list[Path] = []
    for entry in entries:
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if entry.name in ("built_in", "local"):
            continue
        if (entry / "manifest.json").is_file():
            out.append(entry)
    return out


def _iter_local_package_dirs(local_root: Path) -> list[Path]:
    """Return package dirs under the user local/ root."""
    if not local_root.exists():
        try:
            local_root.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning(
                "[extension_package_manager] failed to create missing package root: %s",
                local_root,
            )
            return []
    try:
        entries = sorted(local_root.iterdir(), key=lambda p: p.name)
    except OSError:
        return []
    return [
        entry
        for entry in entries
        if entry.is_dir() and not entry.name.startswith(".")
    ]


def _list_equipment_cards(
    *,
    kind: str,
    package_type: str,
    local_root: Path,
    built_in_root: Path,
    marketplace_by_id: dict[str, dict],
) -> list[dict]:
    """List cards from resources shelf + local/; marketplace provides state."""
    resource_dirs = _iter_resource_package_dirs(kind)
    resource_ids = {pkg.name for pkg in resource_dirs}
    cards: list[dict] = []

    for pkg_dir in resource_dirs:
        card = _build_list_card(
            pkg_dir,
            package_type=package_type,
            source="builtin",
            marketplace=marketplace_by_id.get(pkg_dir.name),
        )
        if card is not None:
            cards.append(card)

    for pkg_dir in _iter_local_package_dirs(local_root):
        if pkg_dir.name in resource_ids:
            raise ValueError(
                f"{package_type} package conflict: {pkg_dir.name} exists in both "
                "local and resources"
            )
        if (built_in_root / pkg_dir.name).is_dir():
            raise ValueError(
                f"{package_type} package conflict: {pkg_dir.name} exists in both "
                "local and built_in"
            )
        card = _build_list_card(
            pkg_dir,
            package_type=package_type,
            source="local",
            marketplace=marketplace_by_id.get(pkg_dir.name),
        )
        if card is not None:
            cards.append(card)
    return cards


def _resolve_show_package_dir(
    name: str,
    *,
    kind_label: str,
    local_root: Path,
    built_in_root: Path,
    resources_root: Path | None,
) -> tuple[Path, str] | None:
    """Resolve show path: local → built_in → resources. Conflict raises."""
    try:
        safe_name = _reject_package_name(name, kind_label)
    except ValueError:
        return None
    local_dir = local_root / safe_name
    built_in_dir = built_in_root / safe_name
    resource_dir = (
        resources_root / safe_name if resources_root is not None else None
    )
    local_exists = local_dir.is_dir()
    built_in_exists = built_in_dir.is_dir()
    resource_exists = resource_dir is not None and resource_dir.is_dir()

    if local_exists and built_in_exists:
        raise ValueError(
            f"{kind_label} package conflict: {safe_name} exists in both local and built_in"
        )
    if local_exists and resource_exists:
        raise ValueError(
            f"{kind_label} package conflict: {safe_name} exists in both local and resources"
        )
    if local_exists:
        return local_dir, "local"
    if built_in_exists:
        return built_in_dir, "builtin"
    if resource_exists:
        return resource_dir, "builtin"
    return None


def _equipment_workspace(agent_workspace: Path | None = None) -> Path:
    """Return the effective equipment workspace root."""
    return agent_workspace if agent_workspace is not None else get_agent_workspace_dir()


def _kind_root(kind: str, *, agent_workspace: Path | None = None) -> Path:
    return _equipment_workspace(agent_workspace) / "plugins" / kind


def _built_in_root(kind: str, *, agent_workspace: Path | None = None) -> Path:
    return _kind_root(kind, agent_workspace=agent_workspace) / "built_in"


def _local_root(kind: str, *, agent_workspace: Path | None = None) -> Path:
    return _kind_root(kind, agent_workspace=agent_workspace) / "local"


def _marketplace_path(kind: str, *, agent_workspace: Path | None = None) -> Path:
    return _kind_root(kind, agent_workspace=agent_workspace) / "marketplace.json"


def _resources_root(kind: str) -> Path | None:
    if kind == _AGENT_TEMPLATE_KIND:
        return get_equipment_resources_agent_templates_dir()
    if kind == _PLUGIN_PACKAGE_KIND:
        return get_equipment_resources_plugin_packages_dir()
    return None


def _read_marketplace_entries(marketplace_path: Path) -> list[dict]:
    if not marketplace_path.is_file():
        return []
    try:
        data = json.loads(marketplace_path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    if not isinstance(data, dict):
        return []
    plugins = data.get("plugins")
    if not isinstance(plugins, list):
        return []
    return [entry for entry in plugins if isinstance(entry, dict)]


def _write_marketplace_entries(marketplace_path: Path, entries: list[dict]) -> None:
    marketplace_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"plugins": entries}
    marketplace_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _read_marketplace(kind: str, *, agent_workspace: Path | None = None) -> list[dict]:
    return _read_marketplace_entries(
        _marketplace_path(kind, agent_workspace=agent_workspace)
    )


def _upsert_marketplace_entry(
    kind: str, package_id: str, *, fields: dict
) -> None:
    entries = _read_marketplace(kind)
    record = {"id": package_id, **fields}
    for entry in entries:
        if entry.get("id") == package_id:
            record = {**entry, **record}
            break
    # Experts and plugins no longer persist `enabled`; ignore legacy keys on write.
    record.pop("enabled", None)
    for index, entry in enumerate(entries):
        if entry.get("id") == package_id:
            entries[index] = record
            break
    else:
        entries.append(record)
    _write_marketplace_entries(_marketplace_path(kind), entries)


def _remove_marketplace_entry(kind: str, package_id: str) -> None:
    entries = _read_marketplace(kind)
    kept = [entry for entry in entries if entry.get("id") != package_id]
    if len(kept) != len(entries):
        _write_marketplace_entries(_marketplace_path(kind), kept)


def read_agent_template_marketplace_entries() -> list[dict]:
    """Read expert marketplace entries."""
    return _read_marketplace(_AGENT_TEMPLATE_KIND)


def read_plugin_marketplace_entries() -> list[dict]:
    """Read plugin marketplace entries."""
    return _read_marketplace(_PLUGIN_PACKAGE_KIND)


def upsert_agent_template_marketplace_entry(
    package_id: str, *, installed: bool, source: str = "local"
) -> None:
    """Update one expert marketplace entry."""
    _upsert_marketplace_entry(
        _AGENT_TEMPLATE_KIND,
        package_id,
        fields={"installed": installed, "source": source},
    )


def upsert_plugin_marketplace_entry(
    package_id: str,
    *,
    installed: bool,
    source: str = "local",
) -> None:
    """Update one plugin marketplace entry."""
    _upsert_marketplace_entry(
        _PLUGIN_PACKAGE_KIND,
        package_id,
        fields={"installed": installed, "source": source},
    )


def remove_agent_template_marketplace_entry(package_id: str) -> None:
    """Remove one expert marketplace entry."""
    _remove_marketplace_entry(_AGENT_TEMPLATE_KIND, package_id)


def remove_plugin_marketplace_entry(package_id: str) -> None:
    """Remove one plugin marketplace entry."""
    _remove_marketplace_entry(_PLUGIN_PACKAGE_KIND, package_id)


def _package_dir_if_present(root: Path, safe_name: str) -> Path | None:
    if not root.is_dir():
        return None
    root_resolved = root.resolve()
    candidate = (root / safe_name).resolve()
    if not candidate.is_relative_to(root_resolved):
        raise ValueError(f"invalid package path (escapes root): {safe_name}")
    if candidate.is_dir():
        return candidate
    return None


def _validate_package_manifest(
    candidate: Path, kind_label: str, package_type: str
) -> Path:
    manifest = _read_package_manifest(candidate)
    if manifest is None:
        raise ValueError(f"{kind_label} package missing/corrupt manifest.json: {candidate.name}")
    declared = manifest.get("packageType")
    if declared != package_type:
        raise ValueError(
            f"{kind_label} package wrong packageType: {candidate.name} "
            f"(expected {package_type}, got {declared!r})"
        )
    return candidate


def _resolve_package_dir(
    name: Any, *, kind: str, kind_label: str, package_type: str
) -> Path:
    safe_name = _reject_package_name(name, kind_label)
    local_dir = _package_dir_if_present(_local_root(kind), safe_name)
    built_in_dir = _package_dir_if_present(_built_in_root(kind), safe_name)
    if local_dir is not None and built_in_dir is not None:
        raise ValueError(
            f"{kind_label} package conflict: {safe_name} exists in both local and built_in"
        )
    if local_dir is not None:
        return _validate_package_manifest(local_dir, kind_label, package_type)
    if built_in_dir is not None:
        return _validate_package_manifest(built_in_dir, kind_label, package_type)
    raise ValueError(f"{kind_label} package not found: {safe_name}")


def resolve_agent_template_dir(name: Any) -> Path:
    """Resolve an installed expert package directory."""
    return _resolve_package_dir(
        name,
        kind=_AGENT_TEMPLATE_KIND,
        kind_label="agent_template",
        package_type="agent_template",
    )


def resolve_plugin_dir(name: Any) -> Path:
    """Resolve an installed plugin package directory."""
    return _resolve_package_dir(
        name,
        kind=_PLUGIN_PACKAGE_KIND,
        kind_label="plugin",
        package_type="plugin",
    )


def read_manifest_version(pkg_dir: Path) -> str:
    """Return stripped manifest.json version, or empty string if missing/invalid."""
    manifest = _read_package_manifest(pkg_dir)
    if manifest is None:
        return ""
    version = manifest.get("version")
    if isinstance(version, str) and version.strip():
        return version.strip()
    return ""


def _build_file_tree(directory: Path, root: Path) -> list[dict]:
    """Build a previewable-only file tree under root."""
    result: list[dict] = []
    try:
        entries = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name))
    except OSError:
        # PermissionError is an OSError subclass — do not list both (G.ERR.09).
        return result
    for entry in entries:
        if entry.name.startswith("."):
            continue
        rel = entry.relative_to(root).as_posix()
        if entry.is_dir():
            children = _build_file_tree(entry, root)
            if children:
                result.append({"path": rel + "/", "type": "dir", "children": children})
        else:
            if _is_previewable_file(rel):
                result.append(
                    {"path": rel, "type": "file", "size": entry.stat().st_size}
                )
    return result


def _reject_preview_path_symlink(pkg_dir: Path, rel: str) -> Path:
    """Resolve a package-relative path, rejecting any symlink segment."""
    pkg_resolved = pkg_dir.resolve()
    cursor = pkg_dir
    for part in Path(rel).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError(f"symlink not allowed: {rel}")
    full_path = cursor.resolve()
    if not full_path.is_relative_to(pkg_resolved):
        raise ValueError(f"path escapes package: {rel}")
    if full_path.is_symlink():
        raise ValueError(f"symlink not allowed: {rel}")
    if not full_path.is_file():
        raise ValueError(f"file not found: {rel}")
    return full_path


# ---------------------------------------------------------------------------
# Lifecycle: create / install / uninstall
# ---------------------------------------------------------------------------


def _require_nonempty_str(params: dict, key: str) -> str:
    """Return a required non-empty string param."""
    value = params.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"missing or invalid {key}")
    return value.strip()


def _require_skill_names(params: dict) -> list[str]:
    """Validate params.skills is a list of existing workspace skill directory names."""
    skills = params.get("skills")
    if not isinstance(skills, list):
        raise ValueError("missing or invalid skills")
    names: list[str] = []
    skills_root = get_agent_skills_dir()
    for item in skills:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("invalid skill name: empty")
        skill_name = _reject_package_name(item.strip(), "skill")
        skill_dir = skills_root / skill_name
        if not skill_dir.is_dir():
            raise ValueError(f"skill not found: {skill_name}")
        names.append(skill_name)
    return names


def _assert_package_id_available(
    package_id: str,
    *,
    local_root: Path,
    built_in_root: Path,
    kind: str,
    resources_root: Path | None = None,
) -> None:
    """Reject create when local/built_in/resources already has the same id."""
    for root, label in ((local_root, "local"), (built_in_root, "built_in")):
        if (root / package_id).exists():
            raise ValueError(f"{kind} package already exists in {label}: {package_id}")
    if resources_root is not None and (resources_root / package_id).is_dir():
        raise ValueError(
            f"{kind} package already exists in resources: {package_id}"
        )


def _skills_manifest_entries(skill_names: list[str]) -> list[dict[str, str]]:
    """Build loader-shaped skills entries with mode fixed to all."""
    return [{"dir": f"./skills/{name}", "mode": "all"} for name in skill_names]


def _copy_workspace_skills(pkg_dir: Path, skill_names: list[str]) -> None:
    """Copy workspace/skills/{name}/ into the new package skills/ tree."""
    if not skill_names:
        return
    skills_root = get_agent_skills_dir()
    for name in skill_names:
        src = skills_root / name
        dst = pkg_dir / "skills" / name
        shutil.copytree(src, dst)


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _lifecycle_package_id(params: dict, kind: str) -> str:
    """Extract and validate params.id for lifecycle RPCs."""
    if not isinstance(params, dict):
        raise ValueError("invalid params")
    return _reject_package_name(params.get("id"), kind)


def _apply_list_source_filter(
    cards: list[dict], params: dict | None
) -> list[dict]:
    """Filter list cards by ``params.filter`` (``builtin`` | ``local``).

    Missing / non-dict params / illegal filter values → return full list.
    """
    if not isinstance(params, dict):
        return cards
    raw = params.get("filter")
    if raw not in ("builtin", "local"):
        return cards
    return [card for card in cards if card.get("source") == raw]


def list_agent_templates(params: dict | None = None) -> list[dict]:
    """List agent_template cards from resources shelf + user local/."""
    market = _marketplace_index(read_agent_template_marketplace_entries())
    cards = _list_equipment_cards(
        kind=_AGENT_TEMPLATE_KIND,
        package_type="agent_template",
        local_root=_local_root(_AGENT_TEMPLATE_KIND),
        built_in_root=_built_in_root(_AGENT_TEMPLATE_KIND),
        marketplace_by_id=market,
    )
    return _apply_list_source_filter(cards, params)


def list_plugin_packages(params: dict | None = None) -> list[dict]:
    """List plugin cards from resources shelf + user local/."""
    market = _marketplace_index(read_plugin_marketplace_entries())
    cards = _list_equipment_cards(
        kind=_PLUGIN_PACKAGE_KIND,
        package_type="plugin",
        local_root=_local_root(_PLUGIN_PACKAGE_KIND),
        built_in_root=_built_in_root(_PLUGIN_PACKAGE_KIND),
        marketplace_by_id=market,
    )
    return _apply_list_source_filter(cards, params)


def show_agent_template(name: str) -> dict | None:
    """Return one agent_template detail card, or None if missing/bad."""
    resolved = _resolve_show_package_dir(
        name,
        kind_label="agent_template",
        local_root=_local_root(_AGENT_TEMPLATE_KIND),
        built_in_root=_built_in_root(_AGENT_TEMPLATE_KIND),
        resources_root=_resources_root(_AGENT_TEMPLATE_KIND),
    )
    if resolved is None:
        return None
    pkg_dir, source = resolved
    market = _marketplace_index(read_agent_template_marketplace_entries())
    marketplace = market.get(pkg_dir.name)
    return _build_show_card(
        pkg_dir,
        package_type="agent_template",
        marketplace=marketplace,
        source=source,
    )


def show_plugin_package(name: str) -> dict | None:
    """Return one plugin detail card, or None if missing/bad."""
    resolved = _resolve_show_package_dir(
        name,
        kind_label="plugin",
        local_root=_local_root(_PLUGIN_PACKAGE_KIND),
        built_in_root=_built_in_root(_PLUGIN_PACKAGE_KIND),
        resources_root=_resources_root(_PLUGIN_PACKAGE_KIND),
    )
    if resolved is None:
        return None
    pkg_dir, source = resolved
    market = _marketplace_index(read_plugin_marketplace_entries())
    marketplace = market.get(pkg_dir.name)
    return _build_show_card(
        pkg_dir,
        package_type="plugin",
        marketplace=marketplace,
        source=source,
    )


def manifest_connector_names(kind: str, package_id: str) -> list[str]:
    """Return deduplicated connector names from a package manifest (read-only).

    Resolves local → built_in → resources (same as show). Does not write disk.
    """
    if kind == _AGENT_TEMPLATE_KIND:
        kind_label = "agent_template"
    elif kind == _PLUGIN_PACKAGE_KIND:
        kind_label = "plugin"
    else:
        raise ValueError(f"unknown package kind: {kind}")

    resolved = _resolve_show_package_dir(
        package_id,
        kind_label=kind_label,
        local_root=_local_root(kind),
        built_in_root=_built_in_root(kind),
        resources_root=_resources_root(kind),
    )
    if resolved is None:
        raise ValueError(f"{kind_label} package not found: {package_id}")
    pkg_dir, _source = resolved
    manifest = _read_package_manifest(pkg_dir)
    if manifest is None:
        raise ValueError(
            f"{kind_label} package missing/corrupt manifest.json: {pkg_dir.name}"
        )

    specs = manifest.get("mcps")
    if specs is None:
        return []
    if not isinstance(specs, list):
        raise ValueError(
            f"{kind_label} package invalid mcps in manifest.json: {pkg_dir.name}"
        )

    names: list[str] = []
    seen: set[str] = set()
    for spec in specs:
        if not isinstance(spec, dict) or "connector" not in spec:
            continue
        connector = spec.get("connector")
        if not isinstance(connector, str) or not connector.strip():
            raise ValueError(
                f"mcp connector entry must be {{'connector': <non-empty str>}}: {spec!r}"
            )
        name = connector.strip()
        if name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def collect_connectors_for_packages(
    *,
    agent_template_id: str | None = None,
    plugin_ids: list[str] | None = None,
    skip_missing: bool = False,
) -> list[str]:
    """Deduplicate connector names across one expert and/or many plugins.

    When ``skip_missing`` is True, packages that raise from
    ``manifest_connector_names`` are skipped (chat.send reconcile). When False,
    errors propagate (install / send gates).
    """
    out: list[str] = []
    seen: set[str] = set()

    def _add(kind: str, package_id: str) -> None:
        try:
            names = manifest_connector_names(kind, package_id)
        except (ValueError, RuntimeError, TypeError, OSError):
            if skip_missing:
                return
            raise
        for name in names:
            if name not in seen:
                seen.add(name)
                out.append(name)

    if isinstance(agent_template_id, str) and agent_template_id.strip():
        _add(_AGENT_TEMPLATE_KIND, agent_template_id.strip())
    for raw in plugin_ids or []:
        if isinstance(raw, str) and raw.strip():
            _add(_PLUGIN_PACKAGE_KIND, raw.strip())
    return out


def unready_connectors(names: list[str]) -> list[str]:
    """Return connector names that are not fully connected (read-only).

    Ready means ``state == "connected"`` only. Missing records, ``connecting``,
    and any other state are unready. Does not call ``connect_mcp`` and does not
    consult ``list_connected_mcps`` (which treats ``connecting`` as live).
    ``enabled=false`` does not make a connected connector unready.
    """
    unready: list[str] = []
    for raw in names:
        name = str(raw or "").strip()
        if not name:
            continue
        rec = get_mcp_record(name)
        if not isinstance(rec, dict) or rec.get("state") != "connected":
            unready.append(name)
    return unready


def install_equipment_gated(kind: str, params: dict) -> tuple[bool, dict[str, Any]]:
    """Gate connector readiness then install. Returns ``(ok, payload)``.

    Pure-read until install: does not call ``connect_mcp``. Unready connectors
    yield ``ok=False`` with ``pending_connectors`` and do not write marketplace.
    """
    if kind == _AGENT_TEMPLATE_KIND:
        kind_label = "agent_template"
        install_fn = install_agent_template
    elif kind == _PLUGIN_PACKAGE_KIND:
        kind_label = "plugin"
        install_fn = install_plugin_package
    else:
        raise ValueError(f"unknown package kind: {kind}")

    package_id = _lifecycle_package_id(params, kind_label)
    pending = unready_connectors(manifest_connector_names(kind, package_id))
    if pending:
        return False, {
            "error": f"connector not connected: {', '.join(pending)}",
            "pending_connectors": list(pending),
        }
    install_fn(params)
    return True, {}


_CONNECTOR_UNINSTALL_NOTICE = (
    "本装备依赖的 connector 仍保持连接，可在 MCP 管理页断开"
)


def uninstall_equipment_with_notice(kind: str, params: dict) -> dict[str, Any]:
    """Uninstall a package; return success payload (connector notice if any).

    Connector MCP records are not recycled on uninstall. When the package
    declared connectors, the success payload includes a ``notice`` tip.
    """
    if kind == _AGENT_TEMPLATE_KIND:
        kind_label = "agent_template"
        uninstall_fn = uninstall_agent_template
    elif kind == _PLUGIN_PACKAGE_KIND:
        kind_label = "plugin"
        uninstall_fn = uninstall_plugin_package
    else:
        raise ValueError(f"unknown package kind: {kind}")

    package_id = _lifecycle_package_id(params, kind_label)
    connectors = manifest_connector_names(kind, package_id)
    uninstall_fn(params)
    if connectors:
        return {"notice": _CONNECTOR_UNINSTALL_NOTICE}
    return {}


def list_agent_template_files(name: str) -> list[dict]:
    """Return the previewable file tree for one agent_template package."""
    pkg_dir = resolve_agent_template_dir(name)
    return _build_file_tree(pkg_dir, pkg_dir)


def read_agent_template_file(name: str, rel_path: str) -> dict:
    """Read one previewable file from an agent_template package."""
    pkg_dir = resolve_agent_template_dir(name)
    rel = str(rel_path or "").strip().replace("\\", "/")
    if not _is_previewable_file(rel):
        raise ValueError(f"file not previewable: {rel}")
    full_path = _reject_preview_path_symlink(pkg_dir, rel)
    size = full_path.stat().st_size
    if size > _MAX_PREVIEW_FILE_BYTES:
        raise ValueError(f"file too large: {rel} ({size} bytes)")
    try:
        content = full_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        content = f"[二进制文件，大小 {size} bytes]"
    return {"path": rel, "content": content}


def create_agent_template(params: dict) -> None:
    """Create a local expert package."""
    if not isinstance(params, dict):
        raise ValueError("invalid params")
    package_id = _reject_package_name(params.get("id"), "agent_template")
    name = _require_nonempty_str(params, "name")
    description = _require_nonempty_str(params, "description")
    persona = _require_nonempty_str(params, "persona")
    skill_names = _require_skill_names(params)

    local_root = _local_root(_AGENT_TEMPLATE_KIND)
    built_in_root = _built_in_root(_AGENT_TEMPLATE_KIND)
    _assert_package_id_available(
        package_id,
        local_root=local_root,
        built_in_root=built_in_root,
        kind="agent_template",
        resources_root=_resources_root(_AGENT_TEMPLATE_KIND),
    )

    pkg_dir = local_root / package_id
    local_root.mkdir(parents=True, exist_ok=True)
    try:
        pkg_dir.mkdir(parents=False, exist_ok=False)
        persona_dir = pkg_dir / "persona"
        persona_dir.mkdir()
        (persona_dir / f"{package_id}.md").write_text(persona, encoding="utf-8")
        _copy_workspace_skills(pkg_dir, skill_names)
        manifest = {
            "packageType": "agent_template",
            "agentCard": {"id": package_id, "name": name, "description": description},
            "persona": {"dir": "./persona"},
            "displayName": {"zh": name, "en": name},
            "displayDescription": {"zh": description, "en": description},
            "skills": _skills_manifest_entries(skill_names),
        }
        _write_json(pkg_dir / "manifest.json", manifest)
    except Exception:
        if pkg_dir.exists():
            shutil.rmtree(pkg_dir, ignore_errors=True)
        raise
    upsert_agent_template_marketplace_entry(
        package_id, installed=False, source="local"
    )


def create_plugin_package(params: dict) -> None:
    """Create a local plugin package."""
    if not isinstance(params, dict):
        raise ValueError("invalid params")
    package_id = _reject_package_name(params.get("id"), "plugin")
    name = _require_nonempty_str(params, "name")
    description = _require_nonempty_str(params, "description")
    skill_names = _require_skill_names(params)

    local_root = _local_root(_PLUGIN_PACKAGE_KIND)
    built_in_root = _built_in_root(_PLUGIN_PACKAGE_KIND)
    _assert_package_id_available(
        package_id,
        local_root=local_root,
        built_in_root=built_in_root,
        kind="plugin",
        resources_root=_resources_root(_PLUGIN_PACKAGE_KIND),
    )

    pkg_dir = local_root / package_id
    local_root.mkdir(parents=True, exist_ok=True)
    try:
        pkg_dir.mkdir(parents=False, exist_ok=False)
        _copy_workspace_skills(pkg_dir, skill_names)
        manifest = {
            "packageType": "plugin",
            "id": package_id,
            "name": name,
            "description": description,
            "displayName": {"zh": name, "en": name},
            "displayDescription": {"zh": description, "en": description},
            "skills": _skills_manifest_entries(skill_names),
        }
        _write_json(pkg_dir / "manifest.json", manifest)
    except Exception:
        if pkg_dir.exists():
            shutil.rmtree(pkg_dir, ignore_errors=True)
        raise
    upsert_plugin_marketplace_entry(
        package_id, installed=False, source="local"
    )


def _install_package(
    package_id: str,
    *,
    kind: str,
    kind_label: str,
    package_type: str,
    is_plugin: bool,
) -> None:
    built_in_root = _built_in_root(kind)
    local_root = _local_root(kind)
    built_in_dir = built_in_root / package_id
    local_dir = local_root / package_id
    resources_root = _resources_root(kind)
    resource_dir = resources_root / package_id if resources_root is not None else None

    if built_in_dir.is_dir() and local_dir.is_dir():
        raise ValueError(
            f"{kind_label} package conflict: {package_id} exists in both local and built_in"
        )
    if built_in_dir.is_dir() or local_dir.is_dir():
        source = "builtin" if built_in_dir.is_dir() else "local"
    else:
        if resource_dir is None or not resource_dir.is_dir():
            raise ValueError(f"{kind_label} package not found: {package_id}")
        _validate_package_manifest(resource_dir, kind_label, package_type)
        built_in_root.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copytree(resource_dir, built_in_dir)
        except Exception:
            if built_in_dir.exists():
                shutil.rmtree(built_in_dir, ignore_errors=True)
            raise
        source = "builtin"

    if is_plugin:
        upsert_plugin_marketplace_entry(
            package_id, installed=True, source=source
        )
    else:
        upsert_agent_template_marketplace_entry(
            package_id, installed=True, source=source
        )


def install_agent_template(params: dict) -> None:
    """Install an expert package."""
    package_id = _lifecycle_package_id(params, "agent_template")
    _install_package(
        package_id,
        kind=_AGENT_TEMPLATE_KIND,
        kind_label="agent_template",
        package_type="agent_template",
        is_plugin=False,
    )


def install_plugin_package(params: dict) -> None:
    """Install a plugin package."""
    package_id = _lifecycle_package_id(params, "plugin")
    _install_package(
        package_id,
        kind=_PLUGIN_PACKAGE_KIND,
        kind_label="plugin",
        package_type="plugin",
        is_plugin=True,
    )


def _locate_user_package_dir(
    package_id: str, *, kind: str, kind_label: str
) -> Path:
    local_dir = _local_root(kind) / package_id
    built_in_dir = _built_in_root(kind) / package_id
    local_exists = local_dir.is_dir()
    built_in_exists = built_in_dir.is_dir()
    if local_exists and built_in_exists:
        raise ValueError(
            f"{kind_label} package conflict: {package_id} exists in both local and built_in"
        )
    if local_exists:
        return local_dir
    if built_in_exists:
        return built_in_dir
    raise ValueError(f"{kind_label} package not found: {package_id}")


def uninstall_agent_template(params: dict) -> None:
    """Uninstall an expert package."""
    package_id = _lifecycle_package_id(params, "agent_template")
    pkg_dir = _locate_user_package_dir(
        package_id, kind=_AGENT_TEMPLATE_KIND, kind_label="agent_template"
    )
    shutil.rmtree(pkg_dir)
    remove_agent_template_marketplace_entry(package_id)


def uninstall_plugin_package(params: dict) -> None:
    """Uninstall a plugin package."""
    package_id = _lifecycle_package_id(params, "plugin")
    pkg_dir = _locate_user_package_dir(
        package_id, kind=_PLUGIN_PACKAGE_KIND, kind_label="plugin"
    )
    shutil.rmtree(pkg_dir)
    remove_plugin_marketplace_entry(package_id)


def is_agent_template_installed(package_id: str) -> bool:
    """Return whether an expert package is installed."""
    for entry in read_agent_template_marketplace_entries():
        if entry.get("id") == package_id:
            return bool(entry.get("installed", False))
    return False


def is_plugin_allowed(package_id: str) -> bool:
    """Return whether a plugin package is installed (legacy ``enabled`` ignored)."""
    for entry in read_plugin_marketplace_entries():
        if entry.get("id") == package_id:
            return bool(entry.get("installed", False))
    return False
