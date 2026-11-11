# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Neutral SkillHub helpers for system plugin packages."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jiuwenswarm.server.runtime.marketplace.hub_asset_port import (
    HubAssetDetail,
    HubAssetQuery,
    HubDownloadRequest,
    create_default_hub_asset_port,
)
from jiuwenswarm.server.runtime.marketplace.hub_package_downloader import HubPackageDownloader

SYSTEM_PLUGIN_KIND = "system_plugin"


def system_plugin_domain(detail: HubAssetDetail) -> str:
    domain = str((detail.package_profile or {}).get("domain") or "agentserver")
    return domain if domain in {"agentserver", "gateway"} else "agentserver"


async def query_system_plugin(asset_id: str, version: str = "") -> HubAssetDetail:
    port = create_default_hub_asset_port()
    return await port.query_asset(
        HubAssetQuery(kind=SYSTEM_PLUGIN_KIND, asset_id=asset_id, version=version)
    )


async def download_system_plugin_archive(detail: HubAssetDetail, destination: Path) -> Path:
    port = create_default_hub_asset_port()
    resolved = await port.resolve_download(
        HubDownloadRequest(
            kind=SYSTEM_PLUGIN_KIND,
            asset_id=detail.asset_id,
            version=detail.version,
        )
    )
    body = await HubPackageDownloader().download_bytes(resolved)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(body)
    return destination


async def _version_files(
    detail: HubAssetDetail, *, with_content: str | None = None
) -> dict[str, Any]:
    reader = getattr(create_default_hub_asset_port(), "list_version_files", None)
    if not callable(reader):
        raise ValueError("Hub file preview is unavailable")
    return await reader(detail.asset_id, detail.version, with_content=with_content)


async def list_system_plugin_files(detail: HubAssetDetail) -> list[tuple[str, int]]:
    """Return package-relative ``(path, size)`` entries from SkillHub."""
    payload = await _version_files(detail)
    entries: list[tuple[str, int]] = []
    for item in payload.get("files") or ():
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        if not isinstance(path, str) or not path.strip():
            continue
        size = item.get("size")
        entries.append((path.strip(), size if isinstance(size, int) else 0))
    return entries


async def read_system_plugin_file(
    detail: HubAssetDetail, path: str
) -> dict[str, Any]:
    """Return one package file's text content; binary files have no content."""
    if path.lower().endswith(".pdf"):
        # The Hub files endpoint returns text only; a PDF has no previewable body.
        return {"path": path, "content": None}
    payload = await _version_files(detail, with_content=path)
    content = payload.get("content")
    return {
        "path": path,
        "content": content if isinstance(content, str) else None,
    }
