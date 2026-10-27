# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""本集群 Hub 上的专家和专家团目录。只读，不看允许名单。"""

from __future__ import annotations

import logging
from typing import Any

from jiuwenswarm.server.runtime.marketplace.hub_asset_port import (
    HubAssetKind,
    HubAssetPort,
    HubAssetSummary,
    HubSearchRequest,
    create_default_hub_asset_port,
)

logger = logging.getLogger(__name__)

_KINDS: tuple[HubAssetKind, ...] = ("agent_template", "agent_group")


async def _remote_assets(port: HubAssetPort, kind: HubAssetKind) -> list[HubAssetSummary]:
    try:
        from jiuwenswarm.server.runtime.marketplace.hub_catalog_cache import (
            cached_asset_catalog,
        )

        items, _state = await cached_asset_catalog(port, kind)
        return [item for item in items if item.kind == kind]
    except Exception:
        logger.warning(
            "hub catalog cache missed for %s; querying Hub directly",
            kind,
            exc_info=True,
        )
    items: list[HubAssetSummary] = []
    page_number = 1
    while page_number <= 100:
        page = await port.search_assets(
            HubSearchRequest(kind=kind, page=page_number, page_size=100)
        )
        items.extend(item for item in page.items if item.kind == kind)
        if not page.items or len(items) >= page.total:
            break
        page_number += 1
    return items


def catalog_rows_from_summaries(items: list[HubAssetSummary]) -> list[dict[str, Any]]:
    """收成管理面目录。id 用 asset_id，同一种类的重复 id 只留第一条。"""
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        kind = str(item.kind or "").strip()
        asset_id = str(item.asset_id or "").strip()
        if kind not in _KINDS or not asset_id or (kind, asset_id) in seen:
            continue
        seen.add((kind, asset_id))
        display_name = str(item.display_name or item.package_name or asset_id).strip()
        rows.append({
            "kind": kind,
            "id": asset_id,
            "display_name": display_name or asset_id,
            "version": str(item.public_latest_version or "").strip(),
            "description": str(item.short_description or "").strip(),
            "package_name": str(item.package_name or "").strip(),
        })
    rows.sort(key=lambda row: (row["kind"], row["id"]))
    return rows


async def list_hub_equipment_catalog(
    port: HubAssetPort | None = None,
) -> dict[str, Any]:
    """读取本进程 Hub 上的专家和专家团。失败时返回空列表和错误说明，不向外抛。"""
    hub = port or create_default_hub_asset_port()
    collected: list[HubAssetSummary] = []
    try:
        for kind in _KINDS:
            collected.extend(await _remote_assets(hub, kind))
    except Exception as exc:
        logger.warning("failed to list Hub equipment catalog", exc_info=True)
        return {"items": [], "error": str(exc)}
    from jiuwenswarm.server.runtime.enterprise_connector_packages import (
        hidden_from_enterprise,
    )

    rows = [
        row
        for row in catalog_rows_from_summaries(collected)
        if not hidden_from_enterprise(str(row.get("id") or ""))
    ]
    return {"items": rows, "error": ""}
