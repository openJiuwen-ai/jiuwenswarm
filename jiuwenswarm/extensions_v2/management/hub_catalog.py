# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Project SkillHub system plugins into the extension page card shape."""

from __future__ import annotations

import logging
from collections.abc import Mapping
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


def _localized_value(value: Any) -> dict[str, str]:
    if isinstance(value, Mapping):
        zh = value.get("zh")
        en = value.get("en")
        zh_text = zh.strip() if isinstance(zh, str) and zh.strip() else ""
        en_text = en.strip() if isinstance(en, str) and en.strip() else ""
        return {"zh": zh_text or en_text, "en": en_text or zh_text}
    return _localized(str(value or ""))


def hub_card(
    item: HubAssetSummary,
    index: StateIndex,
    store: ExtensionInstallState | None = None,
) -> dict[str, Any]:
    package_name = item.package_name or item.asset_id
    record = _hub_record(store, item.asset_id, package_name)
    container = None
    if store is not None:
        container = store.active_bundle(package_name)
    installed = bool(record) or package_name in index.installed or package_name in index.active or container is not None
    active = package_name in index.active or bool(
        container and container.active and container.status in {"active", "partial"}
    )
    # A standby copy is an enabled-but-not-loaded install; the switch follows
    # the requested state so the Hub card matches "My Extensions".
    record_enabled = bool(record.enabled) and record.status != "failed" if record else True
    enabled = active or record_enabled
    kind, members_count, overall = _asset_state(
        container, store, installed, active, enabled,
        record_status=record.status if record else "",
    )
    if container is not None:
        # A bundle has no standalone record; its switch follows the container,
        # otherwise a disabled bundle would fall back to the record-less default.
        enabled = active and overall in {"enabled", "partial"}
    installed_version_value = installed_version(index, package_name) or (
        record.version if record is not None else ""
    ) or (container.version if container is not None else "")
    return {
        "id": item.asset_id,
        "assetId": item.asset_id,
        "packageId": package_name,
        "packageName": package_name,
        "version": item.public_latest_version,
        "installedVersion": installed_version_value,
        "displayName": _localized(item.display_name),
        "displayDescription": _localized(item.short_description),
        "tags": [_localized(tag) for tag in item.tags],
        "avatar": item.icon_uri or None,
        "source": "hub",
        "origin": "hub",
        "kind": kind,
        "membersCount": members_count,
        "overallStatus": overall,
        "installed": installed,
        "active": active,
        "enabled": enabled,
    }


async def list_hub_system_plugins(
    state: Any,
    install_dir: Path | None = None,
    *,
    refresh: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Return Hub cards and cache state, degrading to no cards when the Hub is unreachable."""

    from jiuwenswarm.server.runtime.marketplace.hub_catalog_cache import cached_asset_catalog

    port = create_default_hub_asset_port()
    try:
        items, cache = await cached_asset_catalog(port, SYSTEM_PLUGIN_KIND, refresh=refresh)
    except Exception:  # noqa: BLE001 - the local catalog must stay usable offline
        logger.warning("[ExtensionsV2] failed to list Hub system plugins", exc_info=True)
        return [], None
    if install_dir is not None:
        store = ExtensionInstallState.for_install_dir(install_dir)
        index = build_state_index(state, store)
    else:
        index = build_state_index(state)
        store = _install_state(index)
    return [hub_card(item, index, store) for item in items], cache


async def get_hub_system_plugin(asset_id: str, version: str = "") -> HubAssetDetail:
    return await query_system_plugin(asset_id, version)


def hub_detail(
    detail: HubAssetDetail,
    state: Any,
    store: ExtensionInstallState | None = None,
) -> dict[str, Any]:
    if store is None:
        store = _install_state(build_state_index(state))
    index = build_state_index(state, store)
    package_name = detail.package_name or detail.asset_id
    record = _hub_record(store, detail.asset_id, package_name)
    profile = detail.package_profile or {}
    container = None
    if store is not None:
        container = store.get_bundle(package_name, detail.version) or store.active_bundle(package_name)
    is_bundle = container is not None or _profile_is_bundle(profile)
    events: list[dict[str, Any]] = []
    raw_events = profile.get("events") if not is_bundle else None
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
    bundle_versions = [
        record.version
        for record in (store.bundles() if store is not None else ())
        if record.bundle_id == package_name
    ]
    versions = merge_versions(
        list(detail.versions) or ([detail.version] if detail.version else []),
        installed_versions(index, package_name),
        bundle_versions,
    )
    active = package_name in index.active or bool(
        container and container.active and container.status in {"active", "partial"}
    )
    # A standby copy is an enabled-but-not-loaded install; the switch follows
    # the requested state so the Hub card matches "My Extensions".
    record_enabled = bool(record.enabled) and record.status != "failed" if record else True
    enabled = active or record_enabled
    installed = (
        bool(record) or package_name in index.installed or package_name in index.active
        or container is not None
    )
    kind, members_count, overall = _asset_state(
        container, store, installed, active, enabled, profile,
        record.status if record else "",
    )
    if container is not None:
        # See hub_card: a disabled bundle must not inherit the record-less default.
        enabled = active and overall in {"enabled", "partial"}
    system_plugins = (
        _profile_system_plugins(profile, store)
        if is_bundle and container is None
        else _container_system_plugins(container, store)
        if is_bundle
        else []
    )
    return {
        "id": detail.asset_id,
        "assetId": detail.asset_id,
        "packageId": package_name,
        "packageName": package_name,
        "version": detail.version,
        "installedVersion": (
            installed_version(index, package_name)
            or (record.version if record is not None else "")
            or (container.version if container is not None and container.active else "")
        ),
        "versions": versions,
        "displayName": _localized(detail.display_name),
        "displayDescription": _localized(detail.short_description),
        "tags": [_localized(tag) for tag in detail.tags],
        "avatar": detail.icon_uri or None,
        "source": "hub",
        "origin": "hub",
        "kind": kind,
        "membersCount": members_count,
        "overallStatus": overall,
        "installed": installed,
        "active": active,
        "enabled": enabled,
        "details": _localized(profile.get("details")),
        "readme": _localized(detail.detail_description),
        "preview": detail.preview_url or None,
        "events": events,
        "systemPlugins": system_plugins,
    }


def _asset_state(
    container: Any,
    store: ExtensionInstallState | None,
    installed: bool,
    active: bool,
    enabled: bool,
    profile: dict[str, Any] | None = None,
    record_status: str = "",
) -> tuple[str, int, str]:
    if container is not None and store is not None:
        from jiuwenswarm.extensions_v2.management.containers import bundle_overall_status

        overall = bundle_overall_status(container, store)
        if not getattr(container, "active", False):
            overall = "disabled"
        return "bundle", len(container.members), overall
    if profile is not None and _profile_is_bundle(profile):
        declared = profile.get("system-plugins")
        count = len(declared) if isinstance(declared, list) else 0
        return "bundle", count, "partial" if installed else "not-installed"
    if not installed:
        return "plugin", 1, "not-installed"
    if record_status == "failed":
        return "plugin", 1, "failed"
    return "plugin", 1, "enabled" if enabled else "disabled"


def _profile_is_bundle(profile: dict[str, Any]) -> bool:
    if profile.get("is_bundle") is True:
        return True
    declared = profile.get("system-plugins")
    return isinstance(declared, list) and bool(declared)


def _profile_system_plugins(
    profile: dict[str, Any],
    store: ExtensionInstallState | None,
) -> list[dict[str, Any]]:
    declared = profile.get("system-plugins")
    if not isinstance(declared, list):
        return []
    result: list[dict[str, Any]] = []
    for item in declared:
        if not isinstance(item, dict):
            continue
        package_id = str(item.get("packageId") or "").strip()
        if not package_id:
            continue
        record = store.get(package_id) if store is not None else None
        result.append(
            {
                "packageId": package_id,
                "displayName": _localized_value(item.get("name") or package_id),
                "displayDescription": _localized_value(item.get("description")),
                "version": "",
                "domain": "",
                "status": record.status if record is not None else "pending",
                "active": bool(record.active) if record is not None else False,
                "enabled": bool(record.enabled) if record is not None else False,
                "error": record.error if record is not None else "",
                "installedVersion": record.version if record is not None else "",
            }
        )
    return result


def _container_system_plugins(
    container: Any,
    store: ExtensionInstallState | None,
) -> list[dict[str, Any]]:
    if container is None:
        return []
    bundle_active = bool(getattr(container, "active", False))
    result: list[dict[str, Any]] = []
    for member in container.members:
        record = (
            store.get_bundle_member(container.bundle_id, container.version, member.package_id)
            if store is not None
            else None
        )
        if not bundle_active:
            status, active, enabled = "disabled", False, False
        else:
            status = record.status if record is not None else "pending"
            active = bool(record.active) if record is not None else False
            enabled = bool(record.enabled) if record is not None else False
        result.append(
            {
                "packageId": member.package_id,
                "displayName": _localized(member.package_id),
                "displayDescription": _localized(""),
                "version": member.version,
                "domain": member.domain,
                "status": status,
                "active": active,
                "enabled": enabled,
                "error": record.error if record is not None else "",
                "installedVersion": record.version if record is not None else "",
            }
        )
    return result


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
