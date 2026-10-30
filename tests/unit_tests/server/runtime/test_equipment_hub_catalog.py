# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Hub 专家目录：两种种类都用 asset_id，Hub 失败时返回空列表和错误。"""

from __future__ import annotations

import asyncio

import pytest

from jiuwenswarm.server.runtime.equipment_hub_catalog import (
    catalog_rows_from_summaries,
    list_hub_equipment_catalog,
)
from jiuwenswarm.server.runtime.marketplace.hub_asset_port import (
    HubAssetSummary,
    HubSearchPage,
)


def _summary(kind: str, asset_id: str, **overrides: str) -> HubAssetSummary:
    fields = {
        "kind": kind,
        "asset_id": asset_id,
        "display_name": overrides.get("display_name", asset_id),
        "short_description": overrides.get("short_description", ""),
        "public_latest_version": overrides.get("public_latest_version", "1.0.0"),
        "icon_uri": "",
        "tags": (),
        "package_name": overrides.get("package_name", ""),
    }
    return HubAssetSummary(**fields)


def test_catalog_rows_keep_both_kinds_and_asset_id() -> None:
    rows = catalog_rows_from_summaries([
        _summary("agent_group", "group-b", display_name="", package_name="团乙"),
        _summary("agent_template", "asset-a", display_name="专家甲", short_description="说明"),
        _summary("agent_template", "asset-a", display_name="重复"),
        _summary("plugin", "not-expert", display_name="插件"),
        _summary("agent_template", "", display_name="空"),
    ])
    assert rows == [
        {
            "kind": "agent_group",
            "id": "group-b",
            "display_name": "团乙",
            "version": "1.0.0",
            "description": "",
            "package_name": "团乙",
        },
        {
            "kind": "agent_template",
            "id": "asset-a",
            "display_name": "专家甲",
            "version": "1.0.0",
            "description": "说明",
            "package_name": "",
        },
    ]


def test_list_catalog_reads_both_kinds(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = {
        "agent_template": HubSearchPage(
            items=(_summary("agent_template", "asset-a", display_name="专家甲"),),
            total=1,
            page=1,
            page_size=100,
        ),
        "agent_group": HubSearchPage(
            items=(_summary("agent_group", "group-b", display_name="团乙"),),
            total=1,
            page=1,
            page_size=100,
        ),
    }
    calls: list[str] = []

    class _Port:
        async def search_assets(self, request):
            calls.append(request.kind)
            return pages[request.kind]

    async def _cache_miss(*_args, **_kwargs):
        raise RuntimeError("cache miss")

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.marketplace.hub_catalog_cache.cached_asset_catalog",
        _cache_miss,
    )
    payload = asyncio.run(list_hub_equipment_catalog(_Port()))
    assert calls == ["agent_template", "agent_group"]
    assert payload["error"] == ""
    assert [item["id"] for item in payload["items"]] == ["group-b", "asset-a"]


def test_enterprise_catalog_omits_connector_packages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hidden = "794db1bfe8884d6897d62d3584894a59"
    pages = {
        "agent_template": HubSearchPage(
            items=(
                _summary("agent_template", "asset-a", display_name="专家甲"),
                _summary("agent_template", hidden, display_name="鸿蒙 App 开发专家"),
            ),
            total=2,
            page=1,
            page_size=100,
        ),
        "agent_group": HubSearchPage(
            items=(
                _summary("agent_group", "group-b", display_name="团乙"),
                _summary("agent_group", "32c223d69b834c35b40bc3935c6f9f67", display_name="旅游规划专家团"),
            ),
            total=2,
            page=1,
            page_size=100,
        ),
    }

    class _Port:
        async def search_assets(self, request):
            return pages[request.kind]

    async def _cache_miss(*_args, **_kwargs):
        raise RuntimeError("cache miss")

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.marketplace.hub_catalog_cache.cached_asset_catalog",
        _cache_miss,
    )
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)
    personal = asyncio.run(list_hub_equipment_catalog(_Port()))
    assert [item["id"] for item in personal["items"]] == [
        "32c223d69b834c35b40bc3935c6f9f67",
        "group-b",
        hidden,
        "asset-a",
    ]

    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    enterprise = asyncio.run(list_hub_equipment_catalog(_Port()))
    assert [item["id"] for item in enterprise["items"]] == ["group-b", "asset-a"]


def test_list_catalog_returns_error_when_hub_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Port:
        async def search_assets(self, request):
            del request
            raise RuntimeError("hub down")

    async def _cache_miss(*_args, **_kwargs):
        raise RuntimeError("cache miss")

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.marketplace.hub_catalog_cache.cached_asset_catalog",
        _cache_miss,
    )
    payload = asyncio.run(list_hub_equipment_catalog(_Port()))
    assert payload == {"items": [], "error": "hub down"}
