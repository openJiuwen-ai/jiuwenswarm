# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Agent_template / plugin package manager (catalog + lifecycle)."""

from __future__ import annotations

import base64
import json
import logging
import re
import shutil
import stat
import tarfile
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

import yaml

from jiuwenswarm.common.utils import (
    get_agent_skills_dir,
    get_agent_workspace_dir,
    get_user_workspace_dir,
)
from jiuwenswarm.server.runtime.mcp.state_store import get_mcp_record
from jiuwenswarm.server.runtime.marketplace.hub_asset_installer import (
    install_hub_asset_package,
)
from jiuwenswarm.server.runtime.marketplace.hub_asset_port import (
    HubAssetDetail,
    HubAssetPort,
    HubAssetQuery,
    HubAssetSummary,
    HubAssetKind,
    HubSearchRequest,
    create_default_hub_asset_port,
)
from jiuwenswarm.server.runtime.marketplace.hub_install_state import (
    HubInstallStateStore,
)

logger = logging.getLogger(__name__)

_CATALOG_ROOT_PREVIEWABLE_FILES: frozenset[str] = frozenset({"README.md", "manifest.json"})
_CATALOG_PREVIEWABLE_DIRS: frozenset[str] = frozenset(
    {"agents", "persona", "skills", "tools", "rails", "subagents"}
)
_CATALOG_PREVIEWABLE_EXTS: frozenset[str] = frozenset({".md", ".py"})
_MAX_PREVIEW_FILE_BYTES = 1 * 1024 * 1024
_MAX_IMPORT_FILE_COUNT = 10_000
_MAX_IMPORT_TOTAL_BYTES = 512 * 1024 * 1024
_MAX_IMPORT_PATH_DEPTH = 32
_AVATAR_MIME = {
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}
_AGENT_TEMPLATE_KIND = "agent_templates"
_AGENT_GROUP_KIND = "agent_groups"
_PLUGIN_PACKAGE_KIND = "plugin_packages"


def _reject_package_name(name: Any, kind: str) -> str:
    """Reject empty, dotted, separator, or absolute package names."""
    raw = str(name or "").strip()
    if not raw:
        raise ValueError(f"invalid {kind} name: empty")
    if raw in (".", ".."):
        raise ValueError(f"invalid {kind} name: {raw}")
    if raw.startswith("."):
        raise ValueError(f"invalid {kind} name (hidden): {raw}")
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


def get_equipment_resources_agent_groups_dir() -> Path | None:
    """Return package resources agent_groups dir, or None if absent."""
    root = _resources_plugins_root()
    if root is None:
        return None
    path = root / _AGENT_GROUP_KIND
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
                "displayName": _i18n(
                    spec.get("display_name"), entry_id.strip()
                ),
                "displayDescription": _i18n(spec.get("display_description")),
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
            override_name = spec.get("display_name")
            override_desc = spec.get("display_description")
            if override_name not in (None, ""):
                dn = override_name
            if override_desc not in (None, ""):
                dd = override_desc
            _add(name, dn, dd, kind="connector")
            continue
        mcp_id = spec.get("id") or spec.get("server_id") or spec.get("name")
        if isinstance(mcp_id, str) and mcp_id.strip():
            _add(
                mcp_id.strip(),
                spec.get("display_name"),
                spec.get("display_description"),
            )
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


def _resolve_package_avatar(pkg_dir: Path, manifest: dict) -> str:
    """Inline manifest avatar as a data URL so the frontend can render <img src>."""
    raw = manifest.get("avatar")
    if not isinstance(raw, str):
        return ""
    rel = raw.strip().replace("\\", "/")
    if not rel or rel.startswith("/") or ".." in PurePosixPath(rel).parts:
        return ""
    mime = _AVATAR_MIME.get(Path(rel).suffix.lower())
    if mime is None:
        return ""
    root = pkg_dir.resolve()
    full = (pkg_dir / rel).resolve()
    try:
        full.relative_to(root)
    except ValueError:
        return ""
    if not full.is_file():
        return ""
    try:
        data = full.read_bytes()
    except OSError:
        return ""
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


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
    if manifest is None or manifest.get("package_type") != package_type:
        return None
    category = manifest.get("category")
    card: dict[str, Any] = {
        "id": pkg_dir.name,
        "displayName": manifest.get("display_name") or pkg_dir.name,
        "displayDescription": manifest.get("display_description") or {},
        "category": category if isinstance(category, str) else "",
        "source": source,
        "avatar": _resolve_package_avatar(pkg_dir, manifest),
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
    if manifest is None or manifest.get("package_type") != package_type:
        return None
    resolved_source = source if source is not None else _source_from_pkg_dir(pkg_dir)
    version = manifest.get("version")
    tags = manifest.get("tags")
    details = _read_readme_details(pkg_dir)
    if package_type == "agent_template":
        description = manifest.get("description")
        details = description if isinstance(description, str) else ""
    card: dict[str, Any] = {
        "id": pkg_dir.name,
        "displayName": manifest.get("display_name") or pkg_dir.name,
        "displayDescription": manifest.get("display_description") or {},
        "source": resolved_source,
        "avatar": _resolve_package_avatar(pkg_dir, manifest),
        "version": version if isinstance(version, str) else "",
        "details": details,
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
    quick = manifest.get("quick_inputs")
    card["quickInputs"] = quick if isinstance(quick, list) else []
    return card


def _iter_resource_package_dirs(kind: str) -> list[Path]:
    """Return valid package dirs under the package resources shelf for kind."""
    if kind == "agent_templates":
        root = get_equipment_resources_agent_templates_dir()
    elif kind == "agent_groups":
        root = get_equipment_resources_agent_groups_dir()
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
    if not local_root.is_dir():
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
    if kind == _AGENT_GROUP_KIND:
        if agent_workspace is not None:
            # ``agent_workspace`` is ``<jiuwenswarm-home>/agent/workspace``.
            home = agent_workspace.parent.parent
        else:
            home = get_user_workspace_dir()
        return home / ".agent_teams" / _AGENT_GROUP_KIND
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
    if kind == _AGENT_GROUP_KIND:
        return get_equipment_resources_agent_groups_dir()
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


def _slim_marketplace_entry(entry: dict) -> dict:
    """Keep only the runtime marketplace contract: id, source, installed."""
    slim: dict[str, Any] = {"id": entry.get("id")}
    source = entry.get("source")
    if isinstance(source, str) and source:
        slim["source"] = source
    slim["installed"] = bool(entry.get("installed", False))
    return slim


def _write_marketplace_entries(marketplace_path: Path, entries: list[dict]) -> None:
    marketplace_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"plugins": [_slim_marketplace_entry(entry) for entry in entries]}
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=marketplace_path.parent,
            prefix=f".{marketplace_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temp_path = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        temp_path.replace(marketplace_path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


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
    # Drop name/description/version/enabled and any other leftover catalog cache.
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


def read_agent_group_marketplace_entries() -> list[dict]:
    """Read AgentGroup marketplace entries."""
    return _read_marketplace(_AGENT_GROUP_KIND)


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


def upsert_agent_group_marketplace_entry(
    package_id: str,
    *,
    installed: bool,
    source: str = "local",
) -> None:
    """Update one AgentGroup marketplace entry."""
    _upsert_marketplace_entry(
        _AGENT_GROUP_KIND,
        package_id,
        fields={"installed": installed, "source": source},
    )


def remove_agent_template_marketplace_entry(package_id: str) -> None:
    """Remove one expert marketplace entry."""
    _remove_marketplace_entry(_AGENT_TEMPLATE_KIND, package_id)


def remove_plugin_marketplace_entry(package_id: str) -> None:
    """Remove one plugin marketplace entry."""
    _remove_marketplace_entry(_PLUGIN_PACKAGE_KIND, package_id)


def remove_agent_group_marketplace_entry(package_id: str) -> None:
    """Remove one AgentGroup marketplace entry."""
    _remove_marketplace_entry(_AGENT_GROUP_KIND, package_id)


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
    declared = manifest.get("package_type")
    if declared != package_type:
        raise ValueError(
            f"{kind_label} package wrong package_type: {candidate.name} "
            f"(expected {package_type}, got {declared!r})"
        )
    return candidate


def _normalize_hub_equipment_manifest(package_root: Path, *, package_type: str) -> None:
    """Translate Hub manifest aliases into Jiuwenswarm's canonical schema."""
    manifest = _read_package_manifest(package_root)
    if manifest is None:
        raise ValueError(
            f"{package_type} package missing/corrupt manifest.json: {package_root.name}"
        )

    changed = False
    aliases = {
        "packageType": "package_type",
        "displayName": "display_name",
        "displayDescription": "display_description",
        "defaultInitInput": "default_init_input",
        "quickInputs": "quick_inputs",
    }
    for hub_field, canonical_field in aliases.items():
        if canonical_field not in manifest and hub_field in manifest:
            manifest[canonical_field] = manifest[hub_field]
            changed = True

    if package_type == "agent_template":
        agent_card = manifest.get("agentCard")
        if isinstance(agent_card, dict):
            if "name" not in manifest and "id" in agent_card:
                manifest["name"] = agent_card["id"]
                changed = True
            if "description" not in manifest and "description" in agent_card:
                manifest["description"] = agent_card["description"]
                changed = True

    if changed:
        (package_root / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


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


def resolve_agent_group_dir(name: Any) -> Path:
    """Resolve one installed AgentGroup package for Team assembly."""
    safe_name = _reject_package_name(name, "agent_group")
    market = _marketplace_index(read_agent_group_marketplace_entries())
    entry = market.get(safe_name)
    if entry is None or not bool(entry.get("installed", False)):
        raise ValueError(f"agent_group package not installed: {safe_name}")
    candidate = _resolve_package_dir(
        safe_name,
        kind=_AGENT_GROUP_KIND,
        kind_label="agent_group",
        package_type="agent_group",
    )
    manifest = _read_package_manifest(candidate)
    if manifest is None:
        raise ValueError(
            f"agent_group package missing/corrupt manifest.json: {safe_name}"
        )
    declared_type = manifest.get("package_type")
    if declared_type != "agent_group":
        raise ValueError(
            f"agent_group package wrong package_type: {safe_name} "
            f"(expected 'agent_group', got {declared_type!r})"
        )
    if manifest.get("name") != safe_name:
        raise ValueError(
            f"agent_group package name mismatch: directory={safe_name!r}, "
            f"manifest={manifest.get('name')!r}"
        )
    return candidate


def _resolve_agent_group_definition_dir(name: Any) -> tuple[Path, str] | None:
    """Resolve an AgentGroup definition for catalog and preview operations."""
    return _resolve_show_package_dir(
        str(name or ""),
        kind_label="agent_group",
        local_root=_local_root(_AGENT_GROUP_KIND),
        built_in_root=_built_in_root(_AGENT_GROUP_KIND),
        resources_root=_resources_root(_AGENT_GROUP_KIND),
    )


def _resolve_agent_template_definition_dir(name: Any) -> tuple[Path, str] | None:
    """Resolve an expert definition, including an uninstalled resource item."""
    return _resolve_show_package_dir(
        str(name or ""),
        kind_label="agent_template",
        local_root=_local_root(_AGENT_TEMPLATE_KIND),
        built_in_root=_built_in_root(_AGENT_TEMPLATE_KIND),
        resources_root=_resources_root(_AGENT_TEMPLATE_KIND),
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
        if entry.name.startswith(".") or entry.is_symlink():
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
# Lifecycle: create / import / install / uninstall
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


def _require_mcp_names(params: dict) -> list[str]:
    """Validate optional params.mcps is a list of connector names."""
    mcps = params.get("mcps")
    if mcps is None:
        return []
    if not isinstance(mcps, list):
        raise ValueError("missing or invalid mcps")
    names: list[str] = []
    seen: set[str] = set()
    for item in mcps:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("invalid mcp name: empty")
        name = _reject_package_name(item.strip(), "mcp")
        if name in seen:
            continue
        names.append(name)
        seen.add(name)
    return names


def _require_quick_inputs(params: dict) -> list[dict[str, str]]:
    """Validate and localize optional Agent quick inputs for the manifest."""
    quick_inputs = params.get("quickInputs")
    if quick_inputs is None:
        return []
    if not isinstance(quick_inputs, list):
        raise ValueError("missing or invalid quickInputs")
    entries: list[dict[str, str]] = []
    for item in quick_inputs:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("invalid quick input: empty")
        prompt = item.strip()
        entries.append({"zh": prompt, "en": prompt})
    return entries


def _require_tags(params: dict) -> list[dict[str, str]]:
    """Validate optional bilingual Agent tags and remove duplicates."""
    tags = params.get("tags")
    if tags is None:
        return []
    if not isinstance(tags, list):
        raise ValueError("missing or invalid tags")
    entries: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in tags:
        if not isinstance(item, dict):
            raise ValueError("invalid tag")
        zh = item.get("zh")
        en = item.get("en")
        if not isinstance(zh, str) or not zh.strip():
            raise ValueError("invalid tag: zh/en must be non-empty strings")
        if not isinstance(en, str) or not en.strip():
            raise ValueError("invalid tag: zh/en must be non-empty strings")
        entry = {"zh": zh.strip(), "en": en.strip()}
        key = (entry["zh"], entry["en"])
        if key in seen:
            continue
        seen.add(key)
        entries.append(entry)
    return entries


def _require_agent_group_tags(params: dict) -> list[dict[str, str]]:
    """Validate AgentGroup tags while preserving the optional frontend id."""
    tags = params.get("tags")
    if tags is None:
        return []
    if not isinstance(tags, list):
        raise ValueError("missing or invalid tags")
    entries: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in tags:
        if not isinstance(item, dict):
            raise ValueError("invalid tag")
        tag_id = item.get("id", "")
        zh = item.get("zh")
        en = item.get("en")
        if not isinstance(tag_id, str):
            raise ValueError("invalid tag: id must be a string")
        if not isinstance(zh, str) or not zh.strip():
            raise ValueError("invalid tag: zh/en must be non-empty strings")
        if not isinstance(en, str) or not en.strip():
            raise ValueError("invalid tag: zh/en must be non-empty strings")
        zh_value = zh.strip()
        en_value = en.strip()
        entry = {"zh": zh_value, "en": en_value}
        if tag_id.strip():
            entry["id"] = tag_id.strip()
        key = (entry.get("id", ""), zh_value, en_value)
        if key in seen:
            continue
        seen.add(key)
        entries.append(entry)
    return entries


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


def _assert_no_symlinks(src: Path) -> None:
    """Reject a package subtree containing any symbolic link."""
    if src.is_symlink():
        raise ValueError(f"symbolic links are not allowed in package: {src.name}")
    try:
        entries = list(src.rglob("*"))
    except OSError as exc:
        raise ValueError(f"failed to inspect package: {src.name}") from exc
    if any(entry.is_symlink() for entry in entries):
        raise ValueError(f"symbolic links are not allowed in package: {src.name}")


def _copytree_without_symlinks(src: Path, dst: Path) -> None:
    """Copy a package subtree after rejecting every symbolic link."""
    _assert_no_symlinks(src)
    shutil.copytree(src, dst)


def _lifecycle_package_id(params: dict, kind: str) -> str:
    """Extract and validate params.id for lifecycle RPCs."""
    if not isinstance(params, dict):
        raise ValueError("invalid params")
    return _reject_package_name(params.get("id"), kind)


def _apply_list_source_filter(
    cards: list[dict], params: dict | None
) -> list[dict]:
    """Filter list cards by the catalog/mine source contract.
    local: source is local or builtin and installed
    builtin: source is builtin
    """
    if not isinstance(params, dict):
        return cards
    raw = params.get("filter")
    if raw not in ("builtin", "local", "builtin+hub", "mine"):
        return cards
    if raw == "builtin":
        return [card for card in cards if card.get("source") == "builtin"]
    if raw == "builtin+hub":
        return [card for card in cards if card.get("source") in {"builtin", "hub"}]
    if raw == "mine":
        return [
            card
            for card in cards
            if card.get("source") == "local" or bool(card.get("installed"))
        ]
    filtered: list[dict] = []
    for card in cards:
        source = card.get("source")
        if source == "local" or (
            source in {"builtin", "hub"} and bool(card.get("installed"))
        ):
            filtered.append(card)
    return filtered


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


def _hub_install_state_store(kind: str) -> HubInstallStateStore:
    return HubInstallStateStore(_kind_root(kind))


def _hub_asset_kind(kind: str) -> HubAssetKind:
    if kind == _AGENT_TEMPLATE_KIND:
        return "agent_template"
    if kind == _PLUGIN_PACKAGE_KIND:
        return "plugin"
    raise ValueError(f"unknown Hub asset kind: {kind}")


def _hub_list_card(item: HubAssetSummary) -> dict[str, Any]:
    package_name = item.package_name or item.asset_id
    return {
        "id": item.asset_id,
        "packageName": package_name,
        "displayName": _i18n(item.display_name, package_name),
        "displayDescription": _i18n(item.short_description),
        "category": "",
        "source": "hub",
        "installed": False,
        "connection_state": "disconnected",
        "avatar": item.icon_uri,
        "tags": [_i18n(tag, tag) for tag in item.tags],
        "version": item.public_latest_version,
    }


async def _list_equipment_with_hub(
    kind: str,
    params: dict | None,
    *,
    hub_port: HubAssetPort | None = None,
) -> list[dict]:
    hub_asset_kind = _hub_asset_kind(kind)
    local_cards = (
        list_agent_templates(None)
        if kind == _AGENT_TEMPLATE_KIND
        else list_plugin_packages(None)
    )
    cards_by_id: dict[str, dict] = {}
    local_package_ids: set[str] = set()
    for card in local_cards:
        package_id = str(card.get("id") or "")
        local_package_ids.add(package_id)
        record = _hub_install_state_store(kind).get_by_package_id(package_id)
        if record is not None and record.kind == hub_asset_kind:
            card = {
                **card,
                "id": record.asset_id,
                "packageName": package_id,
                "source": "hub",
                "installedVersion": record.version,
            }
            cards_by_id[record.asset_id] = card
        else:
            cards_by_id[package_id] = card

    source_filter = params.get("filter") if isinstance(params, dict) else None
    if source_filter in {"local", "mine"}:
        return _apply_list_source_filter(list(cards_by_id.values()), params)

    port = hub_port or create_default_hub_asset_port()
    try:
        remote_items: list[HubAssetSummary] = []
        page_number = 1
        while page_number <= 100:
            page = await port.search_assets(
                HubSearchRequest(
                    kind=hub_asset_kind,
                    page=page_number,
                    page_size=100,
                )
            )
            remote_items.extend(page.items)
            if not page.items or len(remote_items) >= page.total:
                break
            page_number += 1
    except Exception:
        logger.warning(
            "[extension_package_manager] failed to list Hub %s packages",
            hub_asset_kind,
            exc_info=True,
        )
        return _apply_list_source_filter(list(cards_by_id.values()), params)
    for item in remote_items:
        if (
            item.kind != hub_asset_kind
            or item.asset_id in cards_by_id
            or (item.package_name or item.asset_id) in local_package_ids
        ):
            continue
        cards_by_id[item.asset_id] = _hub_list_card(item)
    cards = [cards_by_id[key] for key in sorted(cards_by_id)]
    return _apply_list_source_filter(cards, params)


async def list_agent_templates_with_hub(
    params: dict | None = None, *, hub_port: HubAssetPort | None = None
) -> list[dict]:
    return await _list_equipment_with_hub(
        _AGENT_TEMPLATE_KIND, params, hub_port=hub_port
    )


async def list_plugin_packages_with_hub(
    params: dict | None = None, *, hub_port: HubAssetPort | None = None
) -> list[dict]:
    return await _list_equipment_with_hub(
        _PLUGIN_PACKAGE_KIND, params, hub_port=hub_port
    )


def list_agent_groups(params: dict | None = None) -> list[dict]:
    """List AgentGroup definitions from resources shelf and user storage."""
    from jiuwenswarm.agents.swarm.agent_group import load_agent_group_package

    resources = {path.name: path for path in _iter_resource_package_dirs(_AGENT_GROUP_KIND)}
    local = {
        path.name: path
        for path in _iter_local_package_dirs(_local_root(_AGENT_GROUP_KIND))
    }
    built_in = {
        path.name: path
        for path in _iter_local_package_dirs(_built_in_root(_AGENT_GROUP_KIND))
    }
    for package_id in sorted(local):
        if package_id in resources:
            raise ValueError(
                f"agent_group package conflict: {package_id} exists in local and resources"
            )
        if package_id in built_in:
            raise ValueError(
                f"agent_group package conflict: {package_id} exists in local and built_in"
            )

    candidates: list[tuple[str, Path]] = [
        ("builtin", package_dir) for package_dir in resources.values()
    ]
    candidates.extend(
        ("builtin", package_dir)
        for package_id, package_dir in built_in.items()
        if package_id not in resources
    )
    candidates.extend(("local", package_dir) for package_dir in local.values())

    market = _marketplace_index(read_agent_group_marketplace_entries())
    cards: list[dict] = []
    for source, package_dir in sorted(candidates, key=lambda item: item[1].name):
        try:
            card = _build_agent_group_card(
                package_dir,
                source=source,
                marketplace=market.get(package_dir.name),
                load_package=load_agent_group_package,
            )
        except (OSError, ValueError):
            logger.warning(
                "Skipping unloadable agent_group package: %s",
                package_dir,
                exc_info=True,
            )
            continue
        cards.append(card)
    return _apply_list_source_filter(cards, params)


def _build_agent_group_card(
    package_dir: Path,
    *,
    source: str,
    marketplace: dict | None,
    load_package: Any,
    include_details: bool = False,
) -> dict:
    """Build the public AgentGroup card without leaking paths or prompts.

    AgentGroup MCP configuration is intentionally not part of the Web contract
    in this release.  Only members and Skills are exposed for selection and
    inspection.
    """
    manifest = _read_package_manifest(package_dir)
    if manifest is None:
        raise ValueError(
            f"agent_group package missing/corrupt manifest.json: {package_dir.name}"
        )
    templates = load_package(package_dir)
    member_templates = manifest.get("member_templates")
    member_templates = member_templates if isinstance(member_templates, dict) else {}
    members: list[dict[str, Any]] = []
    for member_id, template in templates.items():
        member_manifest = _read_package_manifest(package_dir / "agents" / member_id) or {}
        raw_template_id = member_templates.get(member_id)
        agent_template_id = (
            raw_template_id.strip()
            if isinstance(raw_template_id, str) and raw_template_id.strip()
            else member_id
        )
        avatar = member_manifest.get("avatar")
        members.append(
            {
                "id": member_id,
                "agentTemplateId": agent_template_id,
                "displayName": _i18n(template.agent_card.name, member_id),
                "displayDescription": _i18n(template.agent_card.description),
                "role": "leader" if member_id == "leader" else "member",
                "avatar": avatar if isinstance(avatar, str) else "",
            }
        )

    skill_dirs: dict[str, Path] = {}
    for template in templates.values():
        for skill in template.skills:
            skill_dir = Path(skill.dir)
            skill_dirs.setdefault(skill_dir.name, skill_dir)
    skills: list[dict] = []
    for skill_id, skill_dir in sorted(skill_dirs.items()):
        metadata = _parse_skill_frontmatter(skill_dir / "SKILL.md")
        skills.append(
            {
                "id": skill_id,
                "displayName": _i18n(metadata.get("name"), skill_id),
                "displayDescription": _i18n(metadata.get("description")),
                "avatar": "",
            }
        )

    package_id = package_dir.name
    installed = bool(marketplace and marketplace.get("installed", False))
    category = manifest.get("category")
    tags = manifest.get("tags")
    avatar = manifest.get("avatar")
    card: dict[str, Any] = {
        "id": package_id,
        "name": package_id,
        "displayName": _i18n(manifest.get("display_name"), package_id),
        "displayDescription": _i18n(
            manifest.get("display_description"),
            str(manifest.get("description") or ""),
        ),
        "category": category if isinstance(category, str) else "",
        "tags": tags if isinstance(tags, list) else [],
        "source": source,
        "installed": installed,
        "avatar": avatar if isinstance(avatar, str) else "",
        "memberCount": len(members),
        "members": members,
        "skills": skills,
        "capabilities": {
            "canUse": installed,
            "canInstall": not installed,
            "canUninstall": installed or source == "local",
            "canPreviewFiles": True,
            "canEdit": False,
            "canPublish": False,
        },
    }
    if include_details:
        version = manifest.get("version")
        quick_inputs = manifest.get("quick_inputs")
        instruction = manifest.get("instruction")
        try:
            updated_at = datetime.fromtimestamp(
                (package_dir / "manifest.json").stat().st_mtime,
                tz=timezone.utc,
            ).isoformat().replace("+00:00", "Z")
        except OSError:
            updated_at = ""
        card.update(
            {
                "version": version if isinstance(version, str) else "",
                "updatedAt": updated_at,
                "details": _read_readme_details(package_dir)
                or str(manifest.get("description") or ""),
                "persona": instruction if isinstance(instruction, str) else "",
                "leaderId": "leader",
                "quickInputs": quick_inputs if isinstance(quick_inputs, list) else [],
            }
        )
    return card


def show_agent_group(name: str) -> dict | None:
    """Return one valid AgentGroup detail card, or ``None`` when absent."""
    from jiuwenswarm.agents.swarm.agent_group import load_agent_group_package

    resolved = _resolve_agent_group_definition_dir(name)
    if resolved is None:
        return None
    package_dir, source = resolved
    market = _marketplace_index(read_agent_group_marketplace_entries())
    # Keep show as strict as Team assembly and list: a corrupt/unloadable group
    # must never be presented as selectable detail data.
    return _build_agent_group_card(
        package_dir,
        source=source,
        marketplace=market.get(package_dir.name),
        load_package=load_agent_group_package,
        include_details=True,
    )


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


def _hub_detail_card(
    detail: HubAssetDetail,
) -> dict[str, Any]:
    return {
        "id": detail.asset_id,
        "packageName": detail.package_name or detail.asset_id,
        "displayName": _i18n(
            detail.display_name, detail.package_name or detail.asset_id
        ),
        "displayDescription": _i18n(detail.short_description),
        "source": "hub",
        "avatar": detail.icon_uri,
        "version": detail.version,
        "details": detail.detail_description or detail.short_description,
        "tags": [_i18n(tag, tag) for tag in detail.tags],
        "skills": [],
        "tools": [],
        "rails": [],
        "mcps": [],
        "installed": False,
        "connection_state": "disconnected",
        "pending_connectors": [],
        "quickInputs": [],
    }


async def _show_equipment_with_hub(
    kind: str, name: str, *, hub_port: HubAssetPort | None = None
) -> dict | None:
    hub_asset_kind = _hub_asset_kind(kind)
    state_store = _hub_install_state_store(kind)
    record = state_store.get(name) or state_store.get_by_package_id(name)
    local_name = record.package_id if record is not None else name
    local_card = (
        show_agent_template(local_name)
        if kind == _AGENT_TEMPLATE_KIND
        else show_plugin_package(local_name)
    )
    pending_hub = bool(
        local_card is not None
        and record is not None
        and record.kind == hub_asset_kind
        and local_card.get("installed") is False
    )
    if local_card is not None and not pending_hub:
        if record is not None and record.kind == hub_asset_kind:
            local_card.update(
                {
                    "id": record.asset_id,
                    "packageName": record.package_id,
                    "source": "hub",
                    "installedVersion": record.version,
                }
            )
        return local_card
    port = hub_port or create_default_hub_asset_port()
    remote_asset_id = record.asset_id if record is not None else name
    remote = await port.query_asset(
        HubAssetQuery(kind=hub_asset_kind, asset_id=remote_asset_id)
    )
    if remote.kind != hub_asset_kind:
        raise ValueError(f"Hub package type mismatch: {name}")
    if remote.asset_id != remote_asset_id:
        raise ValueError(
            f"Hub asset id mismatch: expected {remote_asset_id!r}, "
            f"got {remote.asset_id!r}"
        )
    return _hub_detail_card(remote)


async def show_agent_template_with_hub(
    name: str, *, hub_port: HubAssetPort | None = None
) -> dict | None:
    return await _show_equipment_with_hub(
        _AGENT_TEMPLATE_KIND, name, hub_port=hub_port
    )


async def show_plugin_package_with_hub(
    name: str, *, hub_port: HubAssetPort | None = None
) -> dict | None:
    return await _show_equipment_with_hub(
        _PLUGIN_PACKAGE_KIND, name, hub_port=hub_port
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


def _equipment_definition_exists(kind: str, package_id: str) -> bool:
    resources = _resources_root(kind)
    candidates = [
        _local_root(kind) / package_id,
        _built_in_root(kind) / package_id,
    ]
    if resources is not None:
        candidates.append(resources / package_id)
    return any(candidate.is_dir() for candidate in candidates)


async def _prepare_hub_equipment(
    kind: str,
    package_id: str,
    *,
    hub_port: HubAssetPort | None = None,
    downloader: Any = None,
) -> str:
    hub_asset_kind = _hub_asset_kind(kind)
    kind_label = hub_asset_kind
    package_type = hub_asset_kind

    def validate_package(package_root: Path, expected_package_id: str) -> None:
        _normalize_hub_equipment_manifest(package_root, package_type=package_type)
        _validate_package_manifest(package_root, kind_label, package_type)
        manifest = _read_package_manifest(package_root)
        if manifest is None:
            raise ValueError(f"{kind_label} package missing/corrupt manifest.json")
        manifest_id = _package_id_from_manifest(
            manifest, package_type=package_type, kind_label=kind_label
        )
        if manifest_id != expected_package_id:
            raise ValueError(
                f"Hub {kind_label} package id mismatch: expected "
                f"{expected_package_id!r}, "
                f"got {manifest_id!r}"
            )

    def reject_conflict(runtime_package_id: str) -> None:
        _assert_package_id_available(
            runtime_package_id,
            local_root=_local_root(kind),
            built_in_root=_built_in_root(kind),
            kind=kind_label,
            resources_root=_resources_root(kind),
        )

    def write_marketplace(record: Any) -> None:
        if kind == _PLUGIN_PACKAGE_KIND:
            upsert_plugin_marketplace_entry(
                record.package_id, installed=False, source="hub"
            )
        else:
            upsert_agent_template_marketplace_entry(
                record.package_id, installed=False, source="hub"
            )

    result = await install_hub_asset_package(
        kind=hub_asset_kind,
        asset_id=package_id,
        destination_root=_local_root(kind),
        state_store=_hub_install_state_store(kind),
        package_name_validator=lambda value: _reject_package_name(
            value, f"Hub {kind_label} package"
        ),
        package_validator=validate_package,
        conflict_validator=reject_conflict,
        on_committed=write_marketplace,
        on_rollback=lambda runtime_package_id: _remove_marketplace_entry(
            kind, runtime_package_id
        ),
        hub_port=hub_port,
        downloader=downloader,
    )
    return result.package_id


async def install_equipment_from_hub_gated(
    kind: str,
    params: dict,
    *,
    hub_port: HubAssetPort | None = None,
    downloader: Any = None,
) -> tuple[bool, dict[str, Any]]:
    kind_label = _hub_asset_kind(kind)
    requested_id = _lifecycle_package_id(params, kind_label)
    state_store = _hub_install_state_store(kind)
    record = state_store.get(requested_id) or state_store.get_by_package_id(
        requested_id
    )
    runtime_package_id = record.package_id if record is not None else requested_id
    if not _equipment_definition_exists(kind, runtime_package_id):
        runtime_package_id = await _prepare_hub_equipment(
            kind,
            requested_id,
            hub_port=hub_port,
            downloader=downloader,
        )
    ok, payload = install_equipment_gated(kind, {"id": runtime_package_id})
    record = state_store.get(requested_id) or state_store.get_by_package_id(
        runtime_package_id
    )
    if record is not None:
        _upsert_marketplace_entry(
            kind,
            runtime_package_id,
            fields={"installed": ok, "source": "hub"},
        )
    return ok, payload


_CONNECTOR_UNINSTALL_NOTICE = (
    "本装备依赖的 connector 仍保持连接，可在 MCP 管理页断开"
)


def uninstall_equipment_with_notice(kind: str, params: dict) -> dict[str, Any]:
    """Uninstall a package; return success payload (connector notice if any).

    Connector MCP records are not recycled on uninstall. When the package
    declared connectors, the success payload includes a ``notice`` tip.
    """
    if kind == _AGENT_TEMPLATE_KIND:
        uninstall_fn = uninstall_agent_template
    elif kind == _PLUGIN_PACKAGE_KIND:
        uninstall_fn = uninstall_plugin_package
    else:
        raise ValueError(f"unknown package kind: {kind}")

    package_id = resolve_equipment_runtime_id(kind, params.get("id"))
    connectors = manifest_connector_names(kind, package_id)
    uninstall_fn(params)
    if connectors:
        return {"notice": _CONNECTOR_UNINSTALL_NOTICE}
    return {}


def resolve_equipment_runtime_id(kind: str, identifier: Any) -> str:
    """Translate a Hub asset UUID to its local runtime package identifier."""
    package_id = _lifecycle_package_id({"id": identifier}, _hub_asset_kind(kind))
    store = _hub_install_state_store(kind)
    record = store.get(package_id) or store.get_by_package_id(package_id)
    return record.package_id if record is not None else package_id


def _installed_agent_template_runtime_id(identifier: Any) -> str:
    package_id = resolve_equipment_runtime_id(_AGENT_TEMPLATE_KIND, identifier)
    record = _hub_install_state_store(_AGENT_TEMPLATE_KIND).get(identifier)
    if record is None:
        record = _hub_install_state_store(
            _AGENT_TEMPLATE_KIND
        ).get_by_package_id(package_id)
    if record is not None:
        marketplace = _marketplace_index(read_agent_template_marketplace_entries())
        entry = marketplace.get(record.package_id)
        if entry is None or entry.get("installed") is not True:
            raise ValueError(f"agent_template not installed: {record.package_id}")
    return package_id


def list_agent_template_files(name: str) -> list[dict]:
    """Return the previewable file tree for one agent_template package."""
    pkg_dir = resolve_agent_template_dir(_installed_agent_template_runtime_id(name))
    return _build_file_tree(pkg_dir, pkg_dir)


def read_agent_template_file(name: str, rel_path: str) -> dict:
    """Read one previewable file from an agent_template package."""
    pkg_dir = resolve_agent_template_dir(_installed_agent_template_runtime_id(name))
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


def list_agent_group_files(name: str) -> list[dict]:
    """Return the previewable file tree for one AgentGroup definition."""
    resolved = _resolve_agent_group_definition_dir(name)
    if resolved is None:
        raise ValueError(f"agent_group not found: {name!r}")
    pkg_dir, _ = resolved
    return _build_file_tree(pkg_dir, pkg_dir)


def read_agent_group_file(name: str, rel_path: str) -> dict:
    """Read one previewable file from an AgentGroup definition."""
    resolved = _resolve_agent_group_definition_dir(name)
    if resolved is None:
        raise ValueError(f"agent_group not found: {name!r}")
    pkg_dir, _ = resolved
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
    mcp_names = _require_mcp_names(params)
    quick_inputs = _require_quick_inputs(params)
    tags = _require_tags(params)

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
            "package_type": "agent_template",
            "name": name,
            "description": description,
            "persona": {"dir": "./persona"},
            "display_name": {"zh": name, "en": name},
            "display_description": {"zh": description, "en": description},
            "skills": _skills_manifest_entries(skill_names),
        }
        if mcp_names:
            manifest["mcps"] = [{"connector": n} for n in mcp_names]
        if quick_inputs:
            manifest["quick_inputs"] = quick_inputs
        if tags:
            manifest["tags"] = tags
        _write_json(pkg_dir / "manifest.json", manifest)
    except Exception:
        if pkg_dir.exists():
            shutil.rmtree(pkg_dir, ignore_errors=True)
        raise
    upsert_agent_template_marketplace_entry(
        package_id, installed=False, source="local"
    )


def _require_agent_group_members(params: dict) -> tuple[str, list[str]]:
    """Validate AgentGroup leader/member expert IDs."""
    raw_leader = params.get("leaderId")
    if not isinstance(raw_leader, str) or not raw_leader.strip():
        raise ValueError("missing or invalid leaderId")
    leader_id = _reject_package_name(raw_leader, "leaderId")
    raw_members = params.get("memberIds")
    if not isinstance(raw_members, list):
        raise ValueError("missing or invalid memberIds")
    members: list[str] = []
    seen: set[str] = set()
    for raw_member in raw_members:
        if not isinstance(raw_member, str) or not raw_member.strip():
            raise ValueError("invalid memberId")
        member_id = _reject_package_name(raw_member, "memberId")
        if member_id == leader_id:
            raise ValueError("leaderId must not appear in memberIds")
        if member_id == "leader":
            raise ValueError("memberId 'leader' is reserved")
        if member_id in seen:
            raise ValueError(f"duplicate memberId: {member_id}")
        seen.add(member_id)
        members.append(member_id)
    return leader_id, members


def _agent_template_source(package_id: str, *, role: str) -> Path:
    """Resolve and validate one expert definition selected for an AgentGroup."""
    from openjiuwen.harness.resources import load_agent_template_package

    resolved = _resolve_agent_template_definition_dir(package_id)
    if resolved is None:
        raise ValueError(f"{role} agent_template not found: {package_id}")
    package_dir, _ = resolved
    manifest = _read_package_manifest(package_dir)
    if manifest is None or manifest.get("package_type") != "agent_template":
        raise ValueError(f"{role} agent_template is invalid: {package_id}")
    try:
        load_agent_template_package(package_dir / "manifest.json")
    except (OSError, TypeError, ValueError) as exc:
        raise ValueError(
            f"{role} agent_template is not Team-compatible: {package_id}"
        ) from exc
    if role == "member" and (package_dir / "AGENT.md").exists():
        raise ValueError(
            f"member agent_template contains unsupported AGENT.md: {package_id}"
        )
    return package_dir


def create_agent_group(params: dict) -> dict:
    """Create an uninstalled AgentGroup definition under .agent_teams."""
    from jiuwenswarm.agents.swarm.agent_group import load_agent_group_package

    if not isinstance(params, dict):
        raise ValueError("invalid params")
    for forbidden in ("model", "mcps", "pluginNames", "apiKey", "apiBase", "path"):
        if forbidden in params:
            raise ValueError(f"unsupported agent_group field: {forbidden}")

    package_id = _reject_package_name(params.get("id"), "agent_group")
    name = _require_nonempty_str(params, "name")
    description = _require_nonempty_str(params, "description")
    persona = _require_nonempty_str(params, "persona")
    leader_id, member_ids = _require_agent_group_members(params)
    skill_names = list(dict.fromkeys(_require_skill_names(params)))
    quick_inputs = _require_quick_inputs(params)
    tags = _require_agent_group_tags(params)
    category = params.get("category", "")
    if not isinstance(category, str):
        raise ValueError("invalid category")
    category = category.strip()

    sources: dict[str, Path] = {
        "leader": _agent_template_source(leader_id, role="leader")
    }
    for member_id in member_ids:
        sources[member_id] = _agent_template_source(member_id, role="member")

    local_root = _local_root(_AGENT_GROUP_KIND)
    built_in_root = _built_in_root(_AGENT_GROUP_KIND)
    _assert_package_id_available(
        package_id,
        local_root=local_root,
        built_in_root=built_in_root,
        kind="agent_group",
        resources_root=_resources_root(_AGENT_GROUP_KIND),
    )

    local_root.mkdir(parents=True, exist_ok=True)
    container = Path(tempfile.mkdtemp(prefix=".agent_group_create_", dir=local_root))
    stage = container / package_id
    destination = local_root / package_id
    try:
        stage.mkdir()
        agents_dir = stage / "agents"
        agents_dir.mkdir()
        for member_name, source in sources.items():
            _copytree_without_symlinks(source, agents_dir / member_name)

        leader_rules = (
            f"# {name}\n\n"
            "你是专家团 Leader。负责理解目标、拆解任务、协调成员、核验结论，"
            "并向用户提交完整的综合结果。\n"
        )
        leader_rules_path = agents_dir / "leader" / "AGENT.md"
        if not leader_rules_path.exists():
            leader_rules_path.write_text(leader_rules, encoding="utf-8")
        if skill_names:
            (stage / "skills").mkdir()
            for skill_name in skill_names:
                _copytree_without_symlinks(
                    get_agent_skills_dir() / skill_name,
                    stage / "skills" / skill_name,
                )

        member_templates = {"leader": leader_id}
        member_templates.update({member_id: member_id for member_id in member_ids})
        manifest: dict[str, Any] = {
            "name": package_id,
            "package_type": "agent_group",
            "description": description,
            "display_name": {"zh": name, "en": name},
            "display_description": {"zh": description, "en": description},
            "instruction": persona,
            "agents": ["leader", *member_ids],
            "member_templates": member_templates,
            "skills": skill_names,
        }
        if category:
            manifest["category"] = category
        if tags:
            manifest["tags"] = tags
        if quick_inputs:
            manifest["quick_inputs"] = quick_inputs
        _write_json(stage / "manifest.json", manifest)
        (stage / "README.md").write_text(
            f"# {name}\n\n{description}\n",
            encoding="utf-8",
        )

        load_agent_group_package(stage)
        stage.replace(destination)
        try:
            upsert_agent_group_marketplace_entry(
                package_id,
                installed=False,
                source="local",
            )
        except Exception:
            _rmtree(destination)
            raise
    finally:
        shutil.rmtree(container, ignore_errors=True)
    return {"id": package_id}


def create_plugin_package(params: dict) -> None:
    """Create a local plugin package."""
    if not isinstance(params, dict):
        raise ValueError("invalid params")
    package_id = _reject_package_name(params.get("id"), "plugin")
    name = _require_nonempty_str(params, "name")
    description = _require_nonempty_str(params, "description")
    skill_names = _require_skill_names(params)
    mcp_names = _require_mcp_names(params)

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
            "package_type": "plugin",
            "id": package_id,
            "name": name,
            "description": description,
            "display_name": {"zh": name, "en": name},
            "display_description": {"zh": description, "en": description},
            "skills": _skills_manifest_entries(skill_names),
        }
        if mcp_names:
            manifest["mcps"] = [{"connector": n} for n in mcp_names]
        _write_json(pkg_dir / "manifest.json", manifest)
    except Exception:
        if pkg_dir.exists():
            shutil.rmtree(pkg_dir, ignore_errors=True)
        raise
    upsert_plugin_marketplace_entry(
        package_id, installed=False, source="local"
    )


def _reject_archive_member_name(name: str) -> None:
    """Reject zip/tar members that are absolute or contain ``..``."""
    raw = (name or "").replace("\\", "/")
    if not raw:
        return
    posix = PurePosixPath(raw)
    if posix.is_absolute() or ".." in posix.parts:
        raise ValueError("archive member contains illegal path")
    if PureWindowsPath(raw).is_absolute():
        raise ValueError("archive member contains illegal path")
    if len(posix.parts) > _MAX_IMPORT_PATH_DEPTH:
        raise ValueError("archive member path is too deep")


def _check_archive_limits(file_count: int, total_bytes: int) -> None:
    """Reject archives that are too large to import safely."""
    if file_count > _MAX_IMPORT_FILE_COUNT:
        raise ValueError("archive contains too many files")
    if total_bytes > _MAX_IMPORT_TOTAL_BYTES:
        raise ValueError("archive uncompressed size is too large")


def _extract_archive(src: Path, dest: Path) -> None:
    """Extract a zip/tar/tar.gz archive into dest after member-path checks."""
    dest.mkdir(parents=True, exist_ok=True)
    name = src.name.lower()
    try:
        if name.endswith(".zip"):
            with zipfile.ZipFile(src, "r") as zf:
                infos = zf.infolist()
                total_bytes = 0
                file_count = 0
                for info in infos:
                    _reject_archive_member_name(info.filename)
                    unix_mode = (info.external_attr >> 16) & 0xFFFF
                    if stat.S_IFMT(unix_mode) == stat.S_IFLNK:
                        raise ValueError("archive symbolic links are not allowed")
                    if not info.is_dir():
                        file_count += 1
                        total_bytes += info.file_size
                    _check_archive_limits(file_count, total_bytes)
                zf.extractall(dest)
            return
        if name.endswith(".tar.gz") or name.endswith(".tar"):
            with tarfile.open(src, "r:*") as tf:
                members = tf.getmembers()
                total_bytes = 0
                file_count = 0
                for member in members:
                    _reject_archive_member_name(member.name)
                    if member.issym() or member.islnk():
                        raise ValueError("archive links are not allowed")
                    if not (member.isfile() or member.isdir()):
                        raise ValueError("archive contains unsupported special file")
                    if member.isfile():
                        file_count += 1
                        total_bytes += member.size
                    _check_archive_limits(file_count, total_bytes)
                tf.extractall(dest)
            return
    except (zipfile.BadZipFile, tarfile.TarError, OSError) as exc:
        raise ValueError(f"failed to extract archive: {src.name}") from exc
    raise ValueError("unsupported archive format")


def _resolve_import_source_path(params: dict) -> Path:
    """Validate params.path: local absolute path, no URL, no symlink."""
    if not isinstance(params, dict):
        raise ValueError("invalid params")
    raw = str(params.get("path") or "").strip()
    if not raw:
        raise ValueError("缺少参数: path")
    if "://" in raw.lower():
        raise ValueError("仅支持本地文件路径，不支持 URL 协议")
    if "\0" in raw:
        raise ValueError("path 包含非法字符")
    src = Path(raw).expanduser()
    if not src.is_absolute():
        raise ValueError("path 仅支持绝对路径")
    if not src.exists():
        raise ValueError(f"路径不存在: {raw}")
    if src.is_symlink():
        raise ValueError(f"path 不支持符号链接: {raw}")
    return src


def _find_package_root(base: Path, kind_label: str) -> Path:
    """Return the package root: this dir, or exactly one child with manifest.json."""
    if (base / "manifest.json").is_file():
        return base
    try:
        children = [entry for entry in base.iterdir() if entry.is_dir()]
    except OSError as exc:
        raise ValueError(f"{kind_label} package missing/corrupt manifest.json") from exc
    candidates = [child for child in children if (child / "manifest.json").is_file()]
    if len(candidates) != 1:
        raise ValueError(f"{kind_label} package missing/corrupt manifest.json")
    return candidates[0]


def _package_id_from_manifest(
    manifest: dict, *, package_type: str, kind_label: str
) -> str:
    """Read the package identifier and reject unsafe directory names."""
    if package_type == "plugin":
        raw = manifest.get("id")
    else:
        raw = manifest.get("name")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{kind_label} package missing id")
    return _reject_package_name(raw.strip(), kind_label)


def _commit_imported_package(
    pkg_root: Path, *, kind: str, kind_label: str, package_type: str
) -> dict:
    """Validate, copy into local/{id}/, and upsert marketplace installed=false."""
    _validate_package_manifest(pkg_root, kind_label, package_type)
    manifest = _read_package_manifest(pkg_root)
    if manifest is None:
        raise ValueError(
            f"{kind_label} package missing/corrupt manifest.json: {pkg_root.name}"
        )
    package_id = _package_id_from_manifest(
        manifest, package_type=package_type, kind_label=kind_label
    )
    if kind == _AGENT_GROUP_KIND:
        from jiuwenswarm.agents.swarm.agent_group import load_agent_group_package

        if pkg_root.name != package_id:
            raise ValueError(
                "agent_group manifest name must match its package directory"
            )
        _assert_no_symlinks(pkg_root)
        load_agent_group_package(pkg_root)
    local_root = _local_root(kind)
    _assert_package_id_available(
        package_id,
        local_root=local_root,
        built_in_root=_built_in_root(kind),
        kind=kind_label,
        resources_root=_resources_root(kind),
    )
    dest = local_root / package_id
    local_root.mkdir(parents=True, exist_ok=True)
    staging_parent = Path(
        tempfile.mkdtemp(prefix=f".{package_id}.staging-", dir=local_root)
    )
    staged = staging_parent / package_id
    try:
        shutil.copytree(pkg_root, staged)
        staged.replace(dest)
        if kind == _PLUGIN_PACKAGE_KIND:
            upsert_plugin_marketplace_entry(package_id, installed=False, source="local")
        elif kind == _AGENT_GROUP_KIND:
            upsert_agent_group_marketplace_entry(
                package_id, installed=False, source="local"
            )
        else:
            upsert_agent_template_marketplace_entry(
                package_id, installed=False, source="local"
            )
    except Exception:
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        try:
            _remove_marketplace_entry(kind, package_id)
        except Exception:
            logger.warning(
                "[extension_package_manager] failed to roll back marketplace entry: %s/%s",
                kind,
                package_id,
                exc_info=True,
            )
        raise
    finally:
        shutil.rmtree(staging_parent, ignore_errors=True)
    return {"id": package_id}


def _import_package_from_path(
    params: dict, *, kind: str, kind_label: str, package_type: str
) -> dict:
    """Import a zip/tar/dir package into local/{id}/. Raises ValueError on failure."""
    src = _resolve_import_source_path(params)
    if src.is_dir():
        pkg_root = _find_package_root(src, kind_label)
        return _commit_imported_package(
            pkg_root, kind=kind, kind_label=kind_label, package_type=package_type
        )
    if src.is_file():
        with tempfile.TemporaryDirectory(prefix="jiuwenswarm_pkg_import_") as tmp:
            extract_dir = Path(tmp)
            _extract_archive(src, extract_dir)
            pkg_root = _find_package_root(extract_dir, kind_label)
            return _commit_imported_package(
                pkg_root, kind=kind, kind_label=kind_label, package_type=package_type
            )
    raise ValueError(f"不支持的路径类型: {src}")


def import_agent_template(params: dict) -> dict:
    """params['path'] -> {'id': package_id}. Raises ValueError on failure."""
    return _import_package_from_path(
        params,
        kind=_AGENT_TEMPLATE_KIND,
        kind_label="agent_template",
        package_type="agent_template",
    )


def import_agent_group(params: dict) -> dict:
    """Import one AgentGroup definition into .agent_teams/agent_groups/local."""
    return _import_package_from_path(
        params,
        kind=_AGENT_GROUP_KIND,
        kind_label="agent_group",
        package_type="agent_group",
    )


def import_plugin_package(params: dict) -> dict:
    """params['path'] -> {'id': package_id}. Raises ValueError on failure."""
    return _import_package_from_path(
        params,
        kind=_PLUGIN_PACKAGE_KIND,
        kind_label="plugin",
        package_type="plugin",
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
    created_built_in = False
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
        created_built_in = True

    try:
        if kind == _AGENT_GROUP_KIND:
            upsert_agent_group_marketplace_entry(
                package_id, installed=True, source=source
            )
        elif is_plugin:
            upsert_plugin_marketplace_entry(
                package_id, installed=True, source=source
            )
        else:
            upsert_agent_template_marketplace_entry(
                package_id, installed=True, source=source
            )
    except Exception:
        if created_built_in:
            shutil.rmtree(built_in_dir, ignore_errors=True)
        raise


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


def install_agent_group(params: dict) -> None:
    """Install a validated AgentGroup definition."""
    from jiuwenswarm.agents.swarm.agent_group import load_agent_group_package

    package_id = _lifecycle_package_id(params, "agent_group")
    resolved = _resolve_agent_group_definition_dir(package_id)
    if resolved is None:
        raise ValueError(f"agent_group package not found: {package_id}")
    package_dir, _ = resolved
    _assert_no_symlinks(package_dir)
    load_agent_group_package(package_dir)
    _install_package(
        package_id,
        kind=_AGENT_GROUP_KIND,
        kind_label="agent_group",
        package_type="agent_group",
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


def _rmtree(path: Path, *, retries: int = 6, delay: float = 0.5) -> None:
    """Delete directory; retry on transient file lock."""
    for attempt in range(retries):
        try:
            shutil.rmtree(path)
            return
        except OSError as exc:
            locked = getattr(exc, "winerror", None) == 32 or isinstance(exc, PermissionError)
            if not locked or attempt + 1 >= retries:
                raise
            time.sleep(delay)


def uninstall_agent_template(params: dict) -> None:
    """Uninstall an expert package."""
    requested_id = _lifecycle_package_id(params, "agent_template")
    store = _hub_install_state_store(_AGENT_TEMPLATE_KIND)
    record = store.get(requested_id) or store.get_by_package_id(requested_id)
    package_id = record.package_id if record is not None else requested_id
    pkg_dir = _locate_user_package_dir(
        package_id, kind=_AGENT_TEMPLATE_KIND, kind_label="agent_template"
    )
    _rmtree(pkg_dir)
    remove_agent_template_marketplace_entry(package_id)
    store.remove(record.asset_id if record is not None else requested_id)


def uninstall_agent_group(params: dict) -> None:
    """Uninstall an AgentGroup definition without touching runtime Teams."""
    package_id = _lifecycle_package_id(params, "agent_group")
    pkg_dir = _locate_user_package_dir(
        package_id,
        kind=_AGENT_GROUP_KIND,
        kind_label="agent_group",
    )
    _rmtree(pkg_dir)
    remove_agent_group_marketplace_entry(package_id)


def uninstall_plugin_package(params: dict) -> None:
    """Uninstall a plugin package."""
    requested_id = _lifecycle_package_id(params, "plugin")
    store = _hub_install_state_store(_PLUGIN_PACKAGE_KIND)
    record = store.get(requested_id) or store.get_by_package_id(requested_id)
    package_id = record.package_id if record is not None else requested_id
    pkg_dir = _locate_user_package_dir(
        package_id, kind=_PLUGIN_PACKAGE_KIND, kind_label="plugin"
    )
    _rmtree(pkg_dir)
    remove_plugin_marketplace_entry(package_id)
    store.remove(record.asset_id if record is not None else requested_id)


def is_agent_template_installed(package_id: str) -> bool:
    """Return whether an expert package is installed."""
    for entry in read_agent_template_marketplace_entries():
        if entry.get("id") == package_id:
            return bool(entry.get("installed", False))
    return False


def is_agent_group_installed(package_id: str) -> bool:
    """Return whether an AgentGroup definition is installed."""
    for entry in read_agent_group_marketplace_entries():
        if entry.get("id") == package_id:
            return bool(entry.get("installed", False))
    return False


def is_plugin_allowed(package_id: str) -> bool:
    """Return whether a plugin package is installed (legacy ``enabled`` ignored)."""
    for entry in read_plugin_marketplace_entries():
        if entry.get("id") == package_id:
            return bool(entry.get("installed", False))
    return False
