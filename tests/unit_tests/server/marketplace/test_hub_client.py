# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from typing import Any

import pytest

from jiuwenswarm.server.runtime.marketplace.hub_client import (
    HttpHubTransport,
    HubClient,
    HubProtocolError,
)
from jiuwenswarm.server.runtime.marketplace.hub_asset_port import (
    HubAssetQuery,
    HubDownloadRequest,
    HubSearchRequest,
)


class RecordingTransport:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[str, dict[str, Any] | None]] = []

    async def get_data(
        self, path: str, *, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.requests.append((path, params))
        return self.responses.pop(0)


def _catalog_payload() -> dict[str, Any]:
    return {
        "items": [
            {
                "asset_id": "sales-expert",
                "asset_type": "plugin",
                "plugin_type": "agent_template",
                "name": "sales-expert",
                "display_name": "销售专家",
                "short_desc": "分析销售数据",
                "latest_version": "1.2.3",
                "public_latest_version": "1.2.3",
                "icon_uri": "https://example.test/icon.png",
                "tags": ["sales", "analysis"],
                "publisher_id": "publisher-1",
                "publisher_name": "OpenJiuwen",
                "install_count": 7,
            }
        ],
        "total": 1,
        "page": 1,
        "page_size": 20,
    }


def test_default_transport_uses_configured_swarmskills_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEAM_SKILLS_HUB_BASE_URL", raising=False)

    transport = HttpHubTransport()

    assert transport.base_url == "https://swarmskills.openjiuwen.com"


@pytest.mark.parametrize(
    "base_url",
    ["ftp://hub.example.test", "http://hub.example.test", "not-a-url"],
)
def test_transport_rejects_unsafe_base_urls(
    base_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TEAM_SKILLS_HUB_ALLOW_INSECURE_HTTP", raising=False)
    with pytest.raises(ValueError, match="base_url"):
        HttpHubTransport(base_url=base_url)


def test_transport_allows_loopback_http_for_development() -> None:
    assert HttpHubTransport(base_url="http://localhost:8080").base_url == (
        "http://localhost:8080"
    )


def test_transport_allows_public_http_only_with_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEAM_SKILLS_HUB_ALLOW_INSECURE_HTTP", "true")

    transport = HttpHubTransport(base_url="http://119.8.233.112:8080")

    assert transport.base_url == "http://119.8.233.112:8080"


@pytest.mark.asyncio
async def test_list_plugins_preserves_requested_plugin_type_and_normalizes_items() -> (
    None
):
    transport = RecordingTransport([_catalog_payload()])
    client = HubClient(transport=transport)

    page = await client.list_plugins("agent_template", page=1, page_size=20)

    assert transport.requests == [
        (
            "/api/v1/plugins",
            {
                "plugin_type": "agent_template",
                "page": 1,
                "page_size": 20,
            },
        )
    ]
    assert page.total == 1
    assert page.items[0].asset_id == "sales-expert"
    assert page.items[0].display_name == "销售专家"
    assert page.items[0].public_latest_version == "1.2.3"
    assert page.items[0].tags == ("sales", "analysis")


@pytest.mark.asyncio
async def test_get_plugin_matches_exact_asset_id_instead_of_first_result() -> None:
    payload = _catalog_payload()
    payload["items"].insert(0, {**payload["items"][0], "asset_id": "other"})
    transport = RecordingTransport([payload])
    client = HubClient(transport=transport)

    item = await client.get_plugin("sales-expert")

    assert item.asset_id == "sales-expert"
    assert transport.requests == [
        (
            "/api/v1/plugins",
            {"asset_id": "sales-expert", "page": 1, "page_size": 100},
        )
    ]


@pytest.mark.asyncio
async def test_get_version_and_artifact_validate_asset_identity() -> None:
    version_payload = {
        "asset_id": "sales-expert",
        "asset_type": "plugin",
        "plugin_type": "agent_template",
        "version": "1.2.3",
        "name": "sales-expert",
        "display_name": "销售专家",
        "short_desc": "分析销售数据",
        "detail_desc": "完整详情",
        "tags": ["sales"],
    }
    artifact_payload = {
        "asset_id": "sales-expert",
        "plugin_type": "agent_template",
        "version": "1.2.3",
        "download_url": "https://downloads.example.test/sales-expert.zip",
        "checksum_sha256": "a" * 64,
    }
    transport = RecordingTransport([version_payload, artifact_payload])
    client = HubClient(transport=transport)

    detail = await client.get_version("sales-expert", "1.2.3")
    artifact = await client.get_artifact("sales-expert", version="1.2.3")

    assert detail.detail_description == "完整详情"
    assert artifact.download_url.endswith("sales-expert.zip")
    assert transport.requests == [
        ("/api/v1/plugins/sales-expert/versions/1.2.3", None),
        (
            "/api/v1/artifacts/sales-expert",
            {"version": "1.2.3", "is_cli_download": False},
        ),
    ]


@pytest.mark.asyncio
async def test_get_artifact_rejects_mismatched_asset_id() -> None:
    transport = RecordingTransport(
        [
            {
                "asset_id": "other",
                "plugin_type": "plugin",
                "version": "1.0.0",
                "download_url": "https://downloads.example.test/other.zip",
            }
        ]
    )
    client = HubClient(transport=transport)

    with pytest.raises(HubProtocolError, match="asset_id"):
        await client.get_artifact("wanted")


@pytest.mark.asyncio
async def test_asset_port_search_maps_local_expert_kind_to_upstream_type() -> None:
    payload = _catalog_payload()
    payload["items"][0]["asset_type"] = "agent-template"
    payload["items"][0]["plugin_type"] = "agent-template"
    transport = RecordingTransport([payload])
    client = HubClient(transport=transport)

    page = await client.search_assets(
        HubSearchRequest(kind="agent_template", query="销售", page=2, page_size=20)
    )

    assert page.items[0].kind == "agent_template"
    assert not hasattr(page.items[0], "plugin_type")
    assert transport.requests == [
        (
            "/api/v1/plugins",
            {
                "plugin_type": "agent-template",
                "search_keyword": "销售",
                "page": 2,
                "page_size": 20,
            },
        )
    ]


@pytest.mark.asyncio
async def test_asset_port_search_keeps_uuid_separate_from_package_name() -> None:
    payload = _catalog_payload()
    payload["items"][0].update(
        {
            "asset_id": "482becff9f044ba9bad9caef2e43b539",
            "asset_type": "agent-template",
            "plugin_type": "agent-template",
            "name": "sales-expert",
        }
    )
    client = HubClient(transport=RecordingTransport([payload]))

    page = await client.search_assets(
        HubSearchRequest(kind="agent_template", page=1, page_size=20)
    )

    assert page.items[0].asset_id == "482becff9f044ba9bad9caef2e43b539"
    assert page.items[0].package_name == "sales-expert"


@pytest.mark.asyncio
async def test_asset_port_search_normalizes_relative_icon_against_hub_base_url() -> None:
    payload = _catalog_payload()
    payload["items"][0].update(
        {
            "asset_type": "agent-template",
            "plugin_type": "agent-template",
            "icon_uri": "icons/sales.png",
        }
    )
    client = HubClient(
        transport=RecordingTransport([payload]),
        base_url="https://hub.example.test/catalog",
    )

    page = await client.search_assets(
        HubSearchRequest(kind="agent_template", page=1, page_size=20)
    )

    assert page.items[0].icon_uri == (
        "https://hub.example.test/catalog/icons/sales.png"
    )


@pytest.mark.asyncio
async def test_asset_port_query_normalizes_relative_icon_and_preserves_absolute_icon() -> None:
    item_payload = _catalog_payload()
    item_payload["items"][0].update(
        {"asset_type": "agent-template", "plugin_type": "agent-template"}
    )
    version_payload = {
        "asset_id": "sales-expert",
        "asset_type": "agent-template",
        "plugin_type": "agent-template",
        "version": "1.2.3",
        "name": "sales-expert",
        "display_name": "销售专家",
        "icon_uri": "/media/sales.png",
    }
    relative = HubClient(
        transport=RecordingTransport([item_payload, version_payload]),
        base_url="https://hub.example.test/catalog",
    )

    detail = await relative.query_asset(
        HubAssetQuery(kind="agent_template", asset_id="sales-expert")
    )

    assert detail.icon_uri == "https://hub.example.test/media/sales.png"

    absolute_payload = _catalog_payload()
    absolute_payload["items"][0].update(
        {"asset_type": "agent-template", "plugin_type": "agent-template"}
    )
    absolute = HubClient(
        transport=RecordingTransport([absolute_payload]),
        base_url="https://hub.example.test/catalog",
    )
    page = await absolute.search_assets(
        HubSearchRequest(kind="agent_template", page=1, page_size=20)
    )
    assert page.items[0].icon_uri == "https://example.test/icon.png"


@pytest.mark.asyncio
async def test_asset_port_supports_agent_mcp_assets() -> None:
    payload = _catalog_payload()
    payload["items"][0].update(
        {
            "asset_id": "mcp-asset-uuid",
            "asset_type": "agent-mcp",
            "plugin_type": "agent-mcp",
            "name": "amap",
        }
    )
    transport = RecordingTransport([payload])
    client = HubClient(transport=transport)

    page = await client.search_assets(
        HubSearchRequest(kind="mcp", page=1, page_size=20)
    )

    assert page.items[0].kind == "mcp"
    assert page.items[0].package_name == "amap"
    assert transport.requests[0][1]["plugin_type"] == "agent-mcp"


@pytest.mark.asyncio
async def test_asset_port_accepts_optional_plugin_type_when_asset_type_identifies_kind() -> None:
    payload = _catalog_payload()
    payload["items"][0]["asset_type"] = "agent-template"
    payload["items"][0].pop("plugin_type")
    client = HubClient(transport=RecordingTransport([payload]))

    page = await client.search_assets(
        HubSearchRequest(kind="agent_template", page=1, page_size=20)
    )

    assert page.items[0].kind == "agent_template"


@pytest.mark.asyncio
async def test_asset_port_rejects_conflicting_explicit_upstream_types() -> None:
    payload = _catalog_payload()
    payload["items"][0]["asset_type"] = "agent-template"
    payload["items"][0]["plugin_type"] = "agent-plugin"
    client = HubClient(transport=RecordingTransport([payload]))

    with pytest.raises(HubProtocolError, match="type"):
        await client.search_assets(
            HubSearchRequest(kind="agent_template", page=1, page_size=20)
        )


@pytest.mark.asyncio
async def test_asset_port_query_returns_public_detail_for_upstream_alias() -> None:
    item_payload = _catalog_payload()
    item_payload["items"][0].update(
        {
            "asset_type": "agent-template",
            "plugin_type": "agent-template",
        }
    )
    version_payload = {
        "asset_id": "sales-expert",
        "asset_type": "agent-template",
        "plugin_type": "agent-template",
        "version": "1.2.3",
        "name": "sales-expert",
        "display_name": "销售专家",
        "short_desc": "分析销售数据",
        "detail_desc": "完整详情",
        "tags": ["sales"],
    }
    transport = RecordingTransport([item_payload, version_payload])
    client = HubClient(transport=transport)

    result = await client.query_asset(
        HubAssetQuery(kind="agent_template", asset_id="sales-expert")
    )

    assert result.asset_id == "sales-expert"
    assert result.kind == "agent_template"
    assert result.version == "1.2.3"


@pytest.mark.asyncio
async def test_asset_port_query_scopes_catalog_lookup_to_requested_hub_type() -> None:
    item_payload = _catalog_payload()
    item_payload["items"][0].update(
        {"asset_type": "agent-template", "plugin_type": "agent-template"}
    )
    version_payload = {
        "asset_id": "sales-expert",
        "asset_type": "agent-template",
        "plugin_type": "agent-template",
        "version": "1.2.3",
        "name": "sales-expert",
        "display_name": "销售专家",
    }
    transport = RecordingTransport([item_payload, version_payload])
    client = HubClient(transport=transport)

    await client.query_asset(
        HubAssetQuery(kind="agent_template", asset_id="sales-expert")
    )

    assert transport.requests[0] == (
        "/api/v1/plugins",
        {
            "asset_id": "sales-expert",
            "plugin_type": "agent-template",
            "page": 1,
            "page_size": 100,
        },
    )


@pytest.mark.asyncio
async def test_asset_port_query_rejects_conflicting_detail_types() -> None:
    item_payload = _catalog_payload()
    item_payload["items"][0].update(
        {"asset_type": "agent-template", "plugin_type": "agent-template"}
    )
    version_payload = {
        "asset_id": "sales-expert",
        "asset_type": "agent-template",
        "plugin_type": "agent-plugin",
        "version": "1.2.3",
        "name": "sales-expert",
        "display_name": "销售专家",
    }
    client = HubClient(
        transport=RecordingTransport([item_payload, version_payload])
    )

    with pytest.raises(HubProtocolError, match="conflicting Hub types"):
        await client.query_asset(
            HubAssetQuery(kind="agent_template", asset_id="sales-expert")
        )


@pytest.mark.asyncio
async def test_asset_port_download_accepts_upstream_plugin_type() -> None:
    transport = RecordingTransport(
        [
            {
                "asset_id": "sales-plugin",
                "asset_type": "agent-plugin",
                "plugin_type": "agent-plugin",
                "version": "2.0.0",
                "download_url": "https://downloads.example.test/sales-plugin.zip",
                "checksum_sha256": "b" * 64,
            }
        ]
    )
    client = HubClient(transport=transport)

    artifact = await client.resolve_download(
        HubDownloadRequest(
            kind="plugin", asset_id="sales-plugin", version="2.0.0"
        )
    )

    assert artifact.kind == "plugin"
    assert not hasattr(artifact, "plugin_type")


@pytest.mark.asyncio
async def test_asset_port_download_requests_raw_package_and_returns_package_name() -> None:
    transport = RecordingTransport(
        [
            {
                "asset_id": "plugin-asset-uuid",
                "asset_type": "agent-plugin",
                "plugin_type": "agent-plugin",
                "name": "sales-plugin",
                "version": "2.0.0",
                "download_url": "https://downloads.example.test/sales-plugin.zip",
                "checksum_sha256": "b" * 64,
            }
        ]
    )
    client = HubClient(transport=transport)

    artifact = await client.resolve_download(
        HubDownloadRequest(
            kind="plugin", asset_id="plugin-asset-uuid", version="2.0.0"
        )
    )

    assert artifact.package_name == "sales-plugin"
    assert transport.requests == [
        (
            "/api/v1/artifacts/plugin-asset-uuid",
            {"version": "2.0.0", "is_cli_download": False},
        )
    ]


@pytest.mark.asyncio
async def test_asset_port_download_rejects_wrong_asset_type_without_plugin_type() -> None:
    transport = RecordingTransport(
        [
            {
                "asset_id": "not-an-expert",
                "asset_type": "agent-mcp",
                "version": "1.0.0",
                "download_url": "https://downloads.example.test/not-an-expert.zip",
                "checksum_sha256": "c" * 64,
            }
        ]
    )
    client = HubClient(transport=transport)

    with pytest.raises(HubProtocolError, match="type mismatch"):
        await client.resolve_download(
            HubDownloadRequest(
                kind="agent_template", asset_id="not-an-expert", version="1.0.0"
            )
        )


@pytest.mark.asyncio
async def test_asset_port_download_rejects_conflicting_types() -> None:
    transport = RecordingTransport(
        [
            {
                "asset_id": "conflicting",
                "asset_type": "agent-template",
                "plugin_type": "agent-plugin",
                "version": "1.0.0",
                "download_url": "https://downloads.example.test/conflicting.zip",
            }
        ]
    )
    client = HubClient(transport=transport)

    with pytest.raises(HubProtocolError, match="conflicting Hub types"):
        await client.resolve_download(
            HubDownloadRequest(
                kind="agent_template", asset_id="conflicting", version="1.0.0"
            )
        )
