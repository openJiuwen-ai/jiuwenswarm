# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SkillHub-backed MCP package catalog and install lifecycle."""

from __future__ import annotations

import shutil
from pathlib import Path, PureWindowsPath
from typing import Any

from jiuwenswarm.common.utils import get_workspace_dir
from jiuwenswarm.server.runtime.marketplace.hub_asset_installer import (
    install_hub_asset_package,
)
from jiuwenswarm.server.runtime.marketplace.hub_package_downloader import (
    HubPackageDownloader,
)
from jiuwenswarm.server.runtime.marketplace.hub_asset_port import (
    HubAssetPort,
    HubAssetQuery,
    HubSearchRequest,
    create_default_hub_asset_port,
)
from jiuwenswarm.server.runtime.marketplace.hub_install_state import (
    HubInstallRecord,  # noqa: F401 - compatibility re-export
    HubInstallStateStore,
)
from jiuwenswarm.server.runtime.mcp import registry
from jiuwenswarm.server.runtime.mcp.package_manifest import load_mcp_package


def _mcp_root() -> Path:
    return get_workspace_dir() / "mcp"


def _builtin_root() -> Path:
    return _mcp_root() / "mcp_builtins"


def _hub_root() -> Path:
    return _mcp_root() / "mcp_hub"


def _state() -> HubInstallStateStore:
    return HubInstallStateStore(_mcp_root())


def _remote_card(item: Any) -> dict[str, Any]:
    return {
        "id": item.asset_id,
        "name": item.package_name,
        "package_name": item.package_name,
        "display_name": item.display_name or item.package_name,
        "description": item.short_description,
        "category": "",
        "integration_type": "",
        "connection_state": "disconnected",
        "has_bundled_skills": False,
        "icon": item.icon_uri or None,
        "source": "hub",
        "installed": False,
        "version": item.public_latest_version,
        "tags": list(item.tags),
    }


async def _all_remote(port: HubAssetPort) -> list[Any]:
    items: list[Any] = []
    seen_asset_ids: set[str] = set()
    for page in range(1, 101):
        result = await port.search_assets(
            HubSearchRequest(kind="mcp", page=page, page_size=100)
        )
        new_items: list[Any] = []
        for item in result.items:
            if item.kind != "mcp" or item.asset_id in seen_asset_ids:
                continue
            seen_asset_ids.add(item.asset_id)
            new_items.append(item)
        if result.items and not new_items:
            raise ValueError(f"Hub MCP pagination page {page} made no progress")
        items.extend(new_items)
        if len(items) >= result.total or not result.items:
            return items
    raise ValueError("Hub MCP pagination exceeded 100 pages")


async def list_mcps_with_hub(
    mcp_filter: str = "builtin",
    *,
    hub_port: HubAssetPort | None = None,
) -> list[dict[str, Any]]:
    """Merge installed packages with ordinary, non-persisted Hub cards."""
    local = registry.list_marketplace_mcps("local" if mcp_filter == "local" else "builtin")
    cards: dict[str, dict[str, Any]] = {}
    installed_asset_ids: set[str] = set()
    for card in local:
        name = str(card.get("name") or "")
        record = _state().get_by_package_id(name)
        if record is not None and record.kind == "mcp":
            installed_asset_ids.add(record.asset_id)
            card = {
                **card,
                "id": record.asset_id,
                "package_name": name,
                "source": "hub",
                "installed": True,
                "version": record.version,
            }
        else:
            card = {**card, "id": name, "package_name": name, "installed": True}
        cards[name] = card
    if mcp_filter == "local":
        return list(cards.values())
    port = hub_port or create_default_hub_asset_port()
    try:
        remote_items = await _all_remote(port)
    except Exception:
        return list(cards.values())
    for item in remote_items:
        if (
            not item.package_name
            or item.asset_id in installed_asset_ids
            or item.package_name in cards
        ):
            continue
        cards[item.package_name] = _remote_card(item)
    return list(cards.values())


async def show_mcp_with_hub(
    identifier: str,
    *,
    hub_port: HubAssetPort | None = None,
) -> dict[str, Any] | None:
    target = str(identifier or "").strip()
    record = _state().get(target) or _state().get_by_package_id(target)
    if record is not None and (_hub_root() / record.package_id).is_dir():
        detail = registry.get_mcp(record.package_id)
        if detail is not None:
            return {
                **detail,
                "id": record.asset_id,
                "package_name": record.package_id,
                "source": "hub",
                "installed": True,
                "version": record.version,
            }
    local = registry.get_mcp(target)
    if local is not None:
        return {**local, "id": target, "package_name": target, "installed": True}
    port = hub_port or create_default_hub_asset_port()
    detail = await port.query_asset(HubAssetQuery(kind="mcp", asset_id=target))
    if detail.kind != "mcp" or detail.asset_id != target:
        raise ValueError("Hub MCP asset identity mismatch")
    return {
        "id": detail.asset_id,
        "name": detail.package_name,
        "package_name": detail.package_name,
        "display_name": detail.display_name or detail.package_name,
        "description": detail.detail_description or detail.short_description,
        "category": "",
        "integration_type": "",
        "connection_state": "disconnected",
        "has_bundled_skills": False,
        "icon": detail.icon_uri or None,
        "source": "hub",
        "installed": False,
        "version": detail.version,
        "tags": list(detail.tags),
        "mcp_spec": None,
        "cli_spec_present": False,
        "bundled_skills": [],
        "skills": [],
        "tools": [],
    }


def _validate_hub_package_name(value: str) -> str:
    package_id = str(value or "").strip()
    if not package_id or package_id in {".", ".."}:
        raise ValueError("Hub MCP package name is invalid")
    if package_id.startswith(".") or "/" in package_id or "\\" in package_id:
        raise ValueError("Hub MCP package name is invalid")
    if Path(package_id).is_absolute() or PureWindowsPath(package_id).is_absolute():
        raise ValueError("Hub MCP package name is invalid")
    return package_id


async def install_hub_mcp(
    asset_id: str,
    *,
    hub_port: HubAssetPort | None = None,
    downloader: HubPackageDownloader | None = None,
) -> dict[str, Any]:
    asset = str(asset_id or "").strip()
    if not asset:
        raise ValueError("Hub MCP asset id is required")
    existing = _state().get(asset)
    if existing is not None and (_hub_root() / existing.package_id).is_dir():
        return {"id": asset, "name": existing.package_id, "installed": True}

    def reject_builtin_conflict(package_id: str) -> None:
        if (_builtin_root() / package_id).exists():
            raise ValueError(f"Hub MCP package conflicts with built-in: {package_id}")

    def validate_package(package_root: Path, expected_package_id: str) -> None:
        load_mcp_package(package_root, expected_id=expected_package_id)

    result = await install_hub_asset_package(
        kind="mcp",
        asset_id=asset,
        destination_root=_hub_root(),
        state_store=_state(),
        package_name_validator=_validate_hub_package_name,
        package_validator=validate_package,
        conflict_validator=reject_builtin_conflict,
        hub_port=hub_port,
        downloader=downloader,
    )
    return {"id": asset, "name": result.package_id, "installed": True}


def uninstall_hub_mcp(identifier: str) -> dict[str, Any]:
    target = str(identifier or "").strip()
    record = _state().get(target) or _state().get_by_package_id(target)
    if record is None or record.kind != "mcp":
        raise KeyError(f"Hub MCP not installed: {target}")
    try:
        registry.disconnect_mcp(record.package_id)
    except (KeyError, ValueError):
        pass
    shutil.rmtree(_mcp_root() / "skills" / record.package_id, ignore_errors=True)
    (_mcp_root() / "credentials" / f"{record.package_id}.json").unlink(
        missing_ok=True
    )
    shutil.rmtree(_hub_root() / record.package_id, ignore_errors=True)
    _state().remove(record.asset_id)
    return {"id": record.asset_id, "name": record.package_id, "removed": True}
