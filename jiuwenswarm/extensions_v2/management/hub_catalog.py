# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Project SkillHub system plugins into the extension page card shape."""

from __future__ import annotations

import logging
from pathlib import Path, PurePosixPath
from typing import Any

from jiuwenswarm.extensions_v2.management.catalog import (
    StateIndex,
    build_state_index,
    installed_versions,
    installed_version,
    merge_versions,
)
from jiuwenswarm.server.runtime.extension_package_manager import preview_file_tree
from jiuwenswarm.server.runtime.marketplace.extension_install_state import (
    ExtensionInstallRecord,
    ExtensionInstallState,
)
from jiuwenswarm.server.runtime.marketplace.hub_asset_port import (
    HubAssetDetail,
    HubAssetSummary,
    create_default_hub_asset_port,
)
from jiuwenswarm.server.runtime.marketplace.hub_extensions import (
    SYSTEM_PLUGIN_KIND,
    list_system_plugin_files,
    query_system_plugin,
    read_system_plugin_file,
)

logger = logging.getLogger(__name__)


def _localized(value: str) -> dict[str, str]:
    text = str(value or "").strip()
    return {"zh": text, "en": text}


def hub_card(
    item: HubAssetSummary,
    index: StateIndex,
    store: ExtensionInstallState | None = None,
) -> dict[str, Any]:
    package_name = item.package_name or item.asset_id
    record = _hub_record(store, item.asset_id, package_name)
    installed = bool(record) or package_name in index.installed or package_name in index.active
    active = package_name in index.active
    return {
        "id": item.asset_id,
        "assetId": item.asset_id,
        "packageId": package_name,
        "packageName": package_name,
        "version": item.public_latest_version,
        "installedVersion": installed_version(index, package_name) or (
            record.version if record is not None else ""
        ),
        "displayName": _localized(item.display_name),
        "displayDescription": _localized(item.short_description),
        "tags": [_localized(tag) for tag in item.tags],
        "avatar": item.icon_uri or None,
        "source": "hub",
        "origin": "hub",
        "installed": installed,
        "active": active,
        "enabled": active or (record.enabled if record else True),
    }


async def list_hub_system_plugins(
    state: Any,
    install_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Return Hub cards, degrading to an empty list when the Hub is unreachable."""

    from jiuwenswarm.server.runtime.marketplace.hub_catalog_cache import cached_asset_catalog

    port = create_default_hub_asset_port()
    try:
        items, _ = await cached_asset_catalog(port, SYSTEM_PLUGIN_KIND)
    except Exception:  # noqa: BLE001 - the local catalog must stay usable offline
        logger.warning("[ExtensionsV2] failed to list Hub system plugins", exc_info=True)
        return []
    index = build_state_index(state)
    store = ExtensionInstallState.for_install_dir(install_dir) if install_dir is not None else _install_state(index)
    return [hub_card(item, index, store) for item in items]


async def get_hub_system_plugin(asset_id: str, version: str = "") -> HubAssetDetail:
    return await query_system_plugin(asset_id, version)


def hub_detail(
    detail: HubAssetDetail,
    state: Any,
    store: ExtensionInstallState | None = None,
) -> dict[str, Any]:
    index = build_state_index(state)
    package_name = detail.package_name or detail.asset_id
    record = _hub_record(store or _install_state(index), detail.asset_id, package_name)
    profile = detail.package_profile or {}
    events: list[dict[str, Any]] = []
    raw_events = profile.get("events")
    if isinstance(raw_events, list):
        for item in raw_events:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            events.append(
                {
                    "name": name.strip(),
                    "description": _localized(item.get("description")),
                }
            )
    versions = merge_versions(
        list(detail.versions) or ([detail.version] if detail.version else []),
        installed_versions(index, package_name),
    )
    active = package_name in index.active
    return {
        "id": detail.asset_id,
        "assetId": detail.asset_id,
        "packageId": package_name,
        "packageName": package_name,
        "version": detail.version,
        "installedVersion": installed_version(index, package_name) or (record.version if record else ""),
        "versions": versions,
        "displayName": _localized(detail.display_name),
        "displayDescription": _localized(detail.short_description),
        "tags": [_localized(tag) for tag in detail.tags],
        "avatar": detail.icon_uri or None,
        "source": "hub",
        "origin": "hub",
        "installed": bool(record) or package_name in index.installed or package_name in index.active,
        "active": active,
        "enabled": active or (record.enabled if record else True),
        "details": _localized(profile.get("details")),
        "readme": _localized(detail.detail_description),
        "preview": detail.preview_url or None,
        "events": events,
    }


def _hub_record(
    store: ExtensionInstallState | None,
    asset_id: str,
    package_name: str,
) -> ExtensionInstallRecord | None:
    if store is None:
        return None
    records = [
        record
        for record in (store.by_asset(asset_id), store.get(package_name))
        if record is not None
    ]
    if not records:
        return None
    return max(records, key=lambda record: (record.active, record.enabled, record.installed_at))


def _install_state(index: StateIndex) -> ExtensionInstallState | None:
    root = next(iter(index.installed.values()), None)
    if root is not None:
        return ExtensionInstallState.for_install_dir(root.parent.parent)
    record = next(iter(index.active.values()), None)
    if record is not None:
        return ExtensionInstallState.for_install_dir(record.root.parent.parent)
    return None


def _package_file_path(path: str) -> str:
    rel = str(path or "").strip().replace("\\", "/")
    relative = PurePosixPath(rel)
    if not rel or relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"invalid package file path: {rel}")
    return rel


async def hub_file_tree(asset_id: str, version: str = "") -> list[dict[str, Any]]:
    detail = await query_system_plugin(asset_id, version)
    return preview_file_tree(await list_system_plugin_files(detail))


async def hub_file_content(
    asset_id: str, path: str, version: str = ""
) -> dict[str, Any]:
    rel = _package_file_path(path)
    detail = await query_system_plugin(asset_id, version)
    return await read_system_plugin_file(detail, rel)
