# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.marketplace.hub_asset_port import (
    HubAssetDetail,
    HubAssetSummary,
    HubResolvedDownload,
    HubSearchPage,
)
from jiuwenswarm.server.runtime.mcp import marketplace


def test_mcp_hub_lifecycle_methods_are_forwarded_by_web_gateway() -> None:
    from agent_gateway.channel_manager.web.app_web_handlers import (
        _FORWARD_NO_LOCAL_HANDLER_METHODS,
        _FORWARD_REQ_METHODS,
    )

    methods = {"mcp.install", "mcp.uninstall", "mcp.sources.providers"}
    assert methods <= _FORWARD_REQ_METHODS
    assert methods <= _FORWARD_NO_LOCAL_HANDLER_METHODS


class FakeHub:
    def __init__(self) -> None:
        self.search_requests = []

    async def search_assets(self, request):
        self.search_requests.append(request)
        return HubSearchPage(
            items=(
                HubAssetSummary(
                    kind="mcp",
                    asset_id="mcp-asset-uuid",
                    package_name="hub-mcp",
                    display_name="Hub MCP",
                    short_description="Remote package",
                    public_latest_version="1.2.0",
                    icon_uri="https://example.test/icon.png",
                    tags=("tools",),
                ),
            ),
            total=1,
            page=request.page,
            page_size=request.page_size,
        )

    async def query_asset(self, request):
        return HubAssetDetail(
            kind="mcp",
            asset_id=request.asset_id,
            package_name="hub-mcp",
            version="1.2.0",
            display_name="Hub MCP",
            short_description="Remote package",
            detail_description="Remote MCP details",
            icon_uri="https://example.test/icon.png",
            tags=("tools",),
        )

    async def resolve_download(self, request):
        return HubResolvedDownload(
            kind="mcp",
            asset_id=request.asset_id,
            package_name="hub-mcp",
            version=request.version,
            download_url="https://example.test/hub-mcp.zip",
            checksum_sha256="abc123",
        )


class FakeDownloader:
    async def download_and_extract(self, artifact, destination: Path):
        package = destination / artifact.package_name
        package.mkdir(parents=True)
        (package / "mcp.json").write_text(
            '{"mcpServers":{"hub-mcp":{"url":"https://example.test/mcp"}}}',
            encoding="utf-8",
        )
        (package / "manifest.json").write_text(
            json.dumps(
                {
                    "version": artifact.version,
                    "package_type": "mcp",
                    "id": artifact.package_name,
                    "name": "Hub MCP",
                    "description": "Remote MCP details",
                    "display_name": {"zh": "Hub MCP", "en": "Hub MCP"},
                    "display_description": {
                        "zh": "Remote MCP details",
                        "en": "Remote MCP details",
                    },
                    "source": "hub",
                    "integration": {"type": "remote-mcp", "file": "mcp.json"},
                    "skills": [],
                }
            ),
            encoding="utf-8",
        )


class InvalidManifestDownloader:
    async def download_and_extract(self, artifact, destination: Path):
        package = destination / artifact.package_name
        package.mkdir(parents=True)
        (package / "manifest.json").write_text(
            '{"package_type":"mcp","id":"wrong-package"}',
            encoding="utf-8",
        )


class RootMcpDownloader:
    async def download_and_extract(self, artifact, destination: Path):
        (destination / "mcp.json").write_text(
            '{"mcpServers":{"hub-mcp":{"url":"https://example.test/mcp"}}}',
            encoding="utf-8",
        )
        (destination / "manifest.json").write_text(
            json.dumps(
                {
                    "version": artifact.version,
                    "package_type": "mcp",
                    "id": artifact.package_name,
                    "name": "Hub MCP",
                    "description": "Remote MCP details",
                    "display_name": {"zh": "Hub MCP", "en": "Hub MCP"},
                    "display_description": {
                        "zh": "Remote MCP details",
                        "en": "Remote MCP details",
                    },
                    "integration": {"type": "remote-mcp", "file": "mcp.json"},
                    "skills": [],
                }
            ),
            encoding="utf-8",
        )


class FailingSearchHub(FakeHub):
    async def search_assets(self, request):
        raise ConnectionError("Hub unavailable")


class RenamedSearchHub(FakeHub):
    async def search_assets(self, request):
        return HubSearchPage(
            items=(
                HubAssetSummary(
                    kind="mcp",
                    asset_id="mcp-asset-uuid",
                    package_name="renamed-hub-mcp",
                    display_name="Renamed Hub MCP",
                    short_description="Remote package",
                    public_latest_version="1.3.0",
                    icon_uri="",
                    tags=(),
                ),
            ),
            total=1,
            page=request.page,
            page_size=request.page_size,
        )


@pytest.mark.anyio
async def test_hub_mcp_list_show_install_and_uninstall(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(marketplace, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(marketplace.registry, "get_workspace_dir", lambda: tmp_path)
    hub = FakeHub()

    cards = await marketplace.list_mcps_with_hub(hub_port=hub)
    assert cards == [
        {
            "id": "mcp-asset-uuid",
            "name": "hub-mcp",
            "package_name": "hub-mcp",
            "display_name": "Hub MCP",
            "description": "Remote package",
            "category": "",
            "integration_type": "",
            "connection_state": "disconnected",
            "has_bundled_skills": False,
            "icon": "https://example.test/icon.png",
            "source": "hub",
            "installed": False,
            "version": "1.2.0",
            "version_id": "1.2.0",
            "tags": ["tools"],
        }
    ]
    detail = await marketplace.show_mcp_with_hub("mcp-asset-uuid", hub_port=hub)
    assert detail["description"] == "Remote MCP details"
    assert "source_id" not in detail
    assert detail["mcp_spec"] is None
    assert detail["skills"] == []
    assert detail["tools"] == []

    installed = await marketplace.install_hub_mcp(
        "mcp-asset-uuid", hub_port=hub, downloader=FakeDownloader()
    )
    assert installed["name"] == "hub-mcp"
    assert installed["source_id"] == "openjiuwen-hub"
    assert (tmp_path / "mcp" / "mcp_hub" / "hub-mcp" / "manifest.json").is_file()
    cards = await marketplace.list_mcps_with_hub(hub_port=hub)
    assert len(cards) == 1
    assert cards[0]["id"] == "mcp-asset-uuid"
    assert cards[0]["installed"] is True

    credentials = tmp_path / "mcp" / "credentials" / "hub-mcp.json"
    credentials.parent.mkdir(parents=True)
    credentials.write_text('{"token":"secret"}', encoding="utf-8")
    monkeypatch.setattr(marketplace.registry, "disconnect_mcp", lambda name: {"removed": True})
    removed = marketplace.uninstall_hub_mcp("mcp-asset-uuid")
    assert removed == {"id": "mcp-asset-uuid", "name": "hub-mcp", "removed": True}
    assert not (tmp_path / "mcp" / "mcp_hub" / "hub-mcp").exists()
    assert not credentials.exists()


@pytest.mark.anyio
async def test_uninstall_hub_mcp_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(marketplace, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(marketplace.registry, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(marketplace.registry, "disconnect_mcp", lambda name: {"removed": True})
    await marketplace.install_hub_mcp(
        "mcp-asset-uuid", hub_port=FakeHub(), downloader=FakeDownloader()
    )

    first = marketplace.uninstall_hub_mcp("mcp-asset-uuid")
    second = marketplace.uninstall_hub_mcp("mcp-asset-uuid")

    assert first["removed"] is True
    assert second == {
        "id": "mcp-asset-uuid",
        "name": "mcp-asset-uuid",
        "removed": False,
        "already_removed": True,
    }


def test_uninstall_hub_mcp_cleans_orphan_package_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(marketplace, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(marketplace.registry, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(marketplace.registry, "disconnect_mcp", lambda name: {"removed": True})
    orphan = tmp_path / "mcp" / "mcp_hub" / "amap"
    orphan.mkdir(parents=True)

    result = marketplace.uninstall_hub_mcp("amap")

    assert result == {"id": "amap", "name": "amap", "removed": True}
    assert not orphan.exists()


@pytest.mark.anyio
async def test_mcp_market_search_forwards_keyword_to_hub(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(marketplace, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(marketplace.registry, "get_workspace_dir", lambda: tmp_path)
    hub = FakeHub()

    await marketplace.list_mcps_with_hub(
        "builtin", hub_port=hub, query="销售 分析", cache_mode="prefer_cache"
    )

    assert len(hub.search_requests) == 1
    assert hub.search_requests[0].kind == "mcp"
    assert hub.search_requests[0].query == "销售 分析"


@pytest.mark.anyio
async def test_mcp_market_search_drops_result_matching_only_internal_asset_id(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(marketplace, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(marketplace.registry, "get_workspace_dir", lambda: tmp_path)
    class IdOnlyMatchHub(FakeHub):
        async def search_assets(self, request):
            self.search_requests.append(request)
            return HubSearchPage(
                items=(
                    HubAssetSummary(
                        kind="mcp",
                        asset_id="93e6e963-0896-4473-8655-09f611bcddc8",
                        package_name="research-connector",
                        display_name="Research Connector",
                        short_description="Search industry reports",
                        public_latest_version="1.0.0",
                        icon_uri="",
                        tags=("research",),
                    ),
                ),
                total=1,
                page=request.page,
                page_size=request.page_size,
            )

    hub = IdOnlyMatchHub()

    cards = await marketplace.list_mcps_with_hub(
        "builtin", hub_port=hub, query="6"
    )

    assert cards == []


@pytest.mark.anyio
async def test_merged_list_follows_builtin_sort_rule(
    tmp_path: Path, monkeypatch
) -> None:
    """Sort merged Hub and built-in cards with the same marketplace rule."""
    monkeypatch.setattr(marketplace, "get_workspace_dir", lambda: tmp_path)

    def fake_local(mcp_filter: str) -> list[dict]:
        assert mcp_filter == "builtin"
        return [
            {"name": "zebra", "source": "built_in"},
            {"name": "huaweiyun-mcp", "source": "built_in"},
            {"name": "apple", "source": "built_in"},
        ]

    monkeypatch.setattr(marketplace.registry, "list_marketplace_mcps", fake_local)

    cards = await marketplace.list_mcps_with_hub(hub_port=FakeHub())

    assert [card["name"] for card in cards] == [
        "huaweiyun-mcp",
        "apple",
        "hub-mcp",
        "zebra",
    ]


@pytest.mark.anyio
async def test_merged_list_prefers_builtin_when_hub_package_conflicts(
    tmp_path: Path, monkeypatch
) -> None:
    """A stale Hub directory cannot override the built-in package source."""
    monkeypatch.setattr(marketplace, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(
        marketplace.registry,
        "list_marketplace_mcps",
        lambda mcp_filter: [{"name": "duplicate", "source": "built_in"}],
    )
    (tmp_path / "mcp" / "mcp_hub" / "duplicate").mkdir(parents=True)
    marketplace._state().upsert(
        marketplace.HubInstallRecord(
            asset_id="mcp-asset-uuid",
            package_id="duplicate",
            kind="mcp",
            version="1.0.0",
            checksum_sha256="abc123",
            installed_at="2026-09-02T00:00:00Z",
        )
    )

    cards = await marketplace.list_mcps_with_hub(hub_port=FakeHub())

    assert len(cards) == 1
    assert cards[0]["source"] == "built_in"
    assert cards[0]["installed"] is True


@pytest.mark.anyio
async def test_show_prefers_builtin_when_hub_package_conflicts(
    tmp_path: Path, monkeypatch
) -> None:
    """A conflicting Hub install cannot override built-in detail resolution."""
    monkeypatch.setattr(marketplace, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(
        marketplace.registry,
        "get_mcp",
        lambda name: {"name": name, "source": "built_in"},
    )
    (tmp_path / "mcp" / "mcp_hub" / "duplicate").mkdir(parents=True)
    marketplace._state().upsert(
        marketplace.HubInstallRecord(
            asset_id="mcp-asset-uuid",
            package_id="duplicate",
            kind="mcp",
            version="1.0.0",
            checksum_sha256="abc123",
            installed_at="2026-09-02T00:00:00Z",
        )
    )

    detail = await marketplace.show_mcp_with_hub("duplicate", hub_port=FakeHub())

    assert detail["source"] == "built_in"
    assert detail["id"] == "duplicate"
    assert detail["installed"] is True


@pytest.mark.anyio
async def test_list_falls_back_to_local_when_hub_is_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(marketplace, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(
        marketplace.registry,
        "list_marketplace_mcps",
        lambda mcp_filter: [{"name": "builtin-mcp", "source": "builtin"}],
    )

    cards = await marketplace.list_mcps_with_hub(hub_port=FailingSearchHub())

    assert cards == [
        {
            "id": "builtin-mcp",
            "name": "builtin-mcp",
            "package_name": "builtin-mcp",
            "source": "builtin",
            "installed": True,
        }
    ]


@pytest.mark.anyio
async def test_stale_install_record_without_package_is_not_marked_installed(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(marketplace, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(marketplace.registry, "get_workspace_dir", lambda: tmp_path)
    marketplace._state().upsert(
        marketplace.HubInstallRecord(
            asset_id="mcp-asset-uuid",
            package_id="hub-mcp",
            kind="mcp",
            version="1.2.0",
            checksum_sha256="abc123",
            installed_at="2026-09-02T00:00:00Z",
        )
    )

    cards = await marketplace.list_mcps_with_hub(hub_port=FakeHub())

    assert cards[0]["installed"] is False


@pytest.mark.anyio
async def test_installed_asset_id_is_not_duplicated_when_remote_name_changes(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(marketplace, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(marketplace.registry, "get_workspace_dir", lambda: tmp_path)
    await marketplace.install_hub_mcp(
        "mcp-asset-uuid", hub_port=FakeHub(), downloader=FakeDownloader()
    )

    cards = await marketplace.list_mcps_with_hub(hub_port=RenamedSearchHub())

    assert len(cards) == 1
    assert cards[0]["id"] == "mcp-asset-uuid"
    assert cards[0]["name"] == "hub-mcp"
    assert cards[0]["installed"] is True


@pytest.mark.anyio
async def test_install_rejects_builtin_name_conflict(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(marketplace, "get_workspace_dir", lambda: tmp_path)
    (tmp_path / "mcp" / "mcp_builtins" / "hub-mcp").mkdir(parents=True)

    with pytest.raises(ValueError, match="conflicts with built-in"):
        await marketplace.install_hub_mcp(
            "mcp-asset-uuid", hub_port=FakeHub(), downloader=FakeDownloader()
        )

    assert not (tmp_path / "mcp" / "mcp_hub" / "hub-mcp").exists()


@pytest.mark.anyio
async def test_invalid_package_rolls_back_without_state(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(marketplace, "get_workspace_dir", lambda: tmp_path)

    with pytest.raises(ValueError):
        await marketplace.install_hub_mcp(
            "mcp-asset-uuid",
            hub_port=FakeHub(),
            downloader=InvalidManifestDownloader(),
        )

    assert not (tmp_path / "mcp" / "mcp_hub" / "hub-mcp").exists()
    assert not (tmp_path / "mcp" / "hub_state.json").exists()


@pytest.mark.anyio
async def test_install_accepts_root_level_raw_mcp_zip(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(marketplace, "get_workspace_dir", lambda: tmp_path)

    installed = await marketplace.install_hub_mcp(
        "mcp-asset-uuid", hub_port=FakeHub(), downloader=RootMcpDownloader()
    )

    assert installed["name"] == "hub-mcp"
    assert (tmp_path / "mcp" / "mcp_hub" / "hub-mcp" / "manifest.json").is_file()


class DuplicatePagingHub(FakeHub):
    def __init__(self) -> None:
        self.calls = 0

    async def search_assets(self, request):
        self.calls += 1
        ids = ("asset-a",) if request.page == 1 else ("asset-a", "asset-b")
        return HubSearchPage(
            items=tuple(
                HubAssetSummary(
                    kind="mcp",
                    asset_id=asset_id,
                    package_name=asset_id,
                    display_name=asset_id,
                    short_description="",
                    public_latest_version="1.0.0",
                    icon_uri="",
                    tags=(),
                )
                for asset_id in ids
            ),
            total=2,
            page=request.page,
            page_size=request.page_size,
        )


@pytest.mark.anyio
async def test_mcp_hub_pagination_deduplicates_asset_ids() -> None:
    hub = DuplicatePagingHub()

    items = await marketplace._all_remote(hub)

    assert [item.asset_id for item in items] == ["asset-a", "asset-b"]
    assert hub.calls == 2


@pytest.mark.anyio
async def test_mcp_hub_pagination_deduplicates_ids_within_one_page() -> None:
    class SamePageDuplicateHub(FakeHub):
        async def search_assets(self, request):
            items = tuple(
                HubAssetSummary(
                    kind="mcp",
                    asset_id=asset_id,
                    package_name=asset_id,
                    display_name=asset_id,
                    short_description="",
                    public_latest_version="1.0.0",
                    icon_uri="",
                    tags=(),
                )
                for asset_id in ("asset-a", "asset-a", "asset-b")
            )
            return HubSearchPage(
                items=items, total=2, page=request.page, page_size=request.page_size
            )

    items = await marketplace._all_remote(SamePageDuplicateHub())

    assert [item.asset_id for item in items] == ["asset-a", "asset-b"]


@pytest.mark.anyio
async def test_mcp_hub_pagination_rejects_page_without_progress() -> None:
    class NoProgressHub(DuplicatePagingHub):
        async def search_assets(self, request):
            self.calls += 1
            item = HubAssetSummary(
                kind="mcp",
                asset_id="asset-a",
                package_name="asset-a",
                display_name="Asset A",
                short_description="",
                public_latest_version="1.0.0",
                icon_uri="",
                tags=(),
            )
            return HubSearchPage(
                items=(item,), total=2, page=request.page, page_size=request.page_size
            )

    with pytest.raises(ValueError, match="made no progress"):
        await marketplace._all_remote(NoProgressHub())


@pytest.mark.anyio
async def test_mcp_hub_pagination_is_bounded_to_100_pages() -> None:
    class EndlessHub(DuplicatePagingHub):
        async def search_assets(self, request):
            self.calls += 1
            item = HubAssetSummary(
                kind="mcp",
                asset_id=f"asset-{request.page}",
                package_name=f"asset-{request.page}",
                display_name=f"Asset {request.page}",
                short_description="",
                public_latest_version="1.0.0",
                icon_uri="",
                tags=(),
            )
            return HubSearchPage(
                items=(item,), total=101, page=request.page, page_size=request.page_size
            )

    hub = EndlessHub()
    with pytest.raises(ValueError, match="100 pages"):
        await marketplace._all_remote(hub)
    assert hub.calls == 100


def test_remote_card_serves_thumbnail_through_cached_url(
    tmp_path: Path, monkeypatch
) -> None:
    """Hub thumbnails (small data URLs) become cacheable URLs, not list bloat."""
    import base64
    from types import SimpleNamespace

    from jiuwenswarm.common.file_download_token import WebFileDownloadManager
    from jiuwenswarm.server.runtime import package_avatars

    monkeypatch.setattr(package_avatars, "get_agent_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(
        package_avatars,
        "get_package_avatar_cache_dir",
        lambda: tmp_path / ".cache" / "package-avatars",
    )
    monkeypatch.setattr(
        WebFileDownloadManager,
        "_instance",
        WebFileDownloadManager(secret="hub-icon-test-secret"),
    )
    package_avatars._AVATAR_URL_CACHE.clear()

    thumbnail = "data:image/png;base64," + base64.b64encode(
        b"\x89PNG\r\n\x1a\nthumbnail"
    ).decode("ascii")
    card = marketplace._remote_card(
        SimpleNamespace(
            asset_id="mcp-asset-uuid",
            package_name="hub-mcp",
            display_name="Hub MCP",
            short_description="Remote package",
            category_name="",
            icon_uri=thumbnail,
            public_latest_version="1.2.0",
            tags=(),
        )
    )

    assert card["icon"].startswith("/file-api/download?token=")
    assert card["icon"].endswith("&inline=1")

@pytest.mark.anyio
async def test_list_routes_by_source_id_via_provider(
    tmp_path: Path, monkeypatch
) -> None:
    """Remote cards come from the requested source_id Provider (D-08)."""
    from jiuwenswarm.extensions.sdk.connector_source import (
        ConnectorCandidate,
        ConnectorSearchResult,
        ConnectorSourceConfig,
        ConnectorSourceProvider,
    )
    from jiuwenswarm.server.runtime.connector import (
        get_connector_source_manager,
        reset_connector_source_manager,
    )

    class FakeProvider(ConnectorSourceProvider):
        source_id = "corp-hub"
        provider_type = "openjiuwen-hub"
        display_name = "Corp Hub"
        capabilities = frozenset({"search", "get_detail", "get_artifact"})

        async def search(self, request, context):
            del request, context
            return ConnectorSearchResult(
                items=(
                    ConnectorCandidate(
                        source_id="corp-hub",
                        asset_id="corp-asset",
                        package_name="corp-mcp",
                        display_name="Corp MCP",
                        version_id="2.0.0",
                        short_description="from corp",
                    ),
                ),
                total=1,
                next_page=None,
            )

        async def get_artifact(self, connector_ref, version_id, context):
            raise NotImplementedError

    monkeypatch.setattr(marketplace, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(marketplace.registry, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(marketplace, "_is_enterprise_edition", lambda: True)
    reset_connector_source_manager()
    runtime = get_connector_source_manager()
    runtime.register_provider(
        ConnectorSourceConfig(
            source_id="corp-hub",
            provider_type="openjiuwen-hub",
            enabled=True,
            capabilities=frozenset({"search", "get_detail", "get_artifact"}),
        ),
        FakeProvider(),
        display_name="Corp Hub",
    )

    # Seed a built-in local card and an installed Hub package from this source
    # that is NOT in the remote page; source-scoped list is remote-only.
    monkeypatch.setattr(
        marketplace.registry,
        "list_marketplace_mcps",
        lambda mcp_filter="builtin": [
            {
                "name": "builtin-mcp",
                "display_name": "Builtin",
                "description": "",
                "category": "",
                "source": "built_in",
                "connection_state": "disconnected",
                "integration_type": "stdio-mcp",
                "has_bundled_skills": False,
            },
            {
                "name": "installed-only-mcp",
                "display_name": "Installed Only",
                "description": "",
                "category": "",
                "source": "hub",
                "connection_state": "disconnected",
                "integration_type": "stdio-mcp",
                "has_bundled_skills": False,
            },
        ]
        if mcp_filter == "builtin"
        else [],
    )
    from jiuwenswarm.server.runtime.marketplace.hub_install_state import HubInstallRecord

    marketplace._state().upsert(
        HubInstallRecord(
            asset_id="installed-only-asset",
            package_id="installed-only-mcp",
            kind="mcp",
            version="1.0.0",
            checksum_sha256="abc",
            installed_at="2026-01-01T00:00:00+00:00",
            source_id="corp-hub",
        )
    )
    marketplace._state().upsert(
        HubInstallRecord(
            asset_id="corp-asset",
            package_id="corp-mcp",
            kind="mcp",
            version="1.5.0",
            checksum_sha256="def",
            installed_at="2026-01-01T00:00:00+00:00",
            source_id="corp-hub",
        )
    )

    cards = await marketplace.list_mcps_with_hub("hub", source_id="corp-hub")
    assert len(cards) == 1
    assert cards[0]["id"] == "corp-asset"
    assert cards[0]["source"] == "hub"
    assert cards[0]["source_id"] == "corp-hub"
    assert cards[0]["name"] == "corp-mcp"
    assert cards[0]["installed"] is True
    assert cards[0]["version"] == "2.0.0"
    assert cards[0]["installed_version"] == "1.5.0"
    assert cards[0]["has_update"] is True
    assert all(item.get("source") == "hub" for item in cards)
    assert all(item.get("source_id") == "corp-hub" for item in cards)
    assert all(item["name"] != "builtin-mcp" for item in cards)
    assert all(item["name"] != "installed-only-mcp" for item in cards)

    providers = await marketplace.list_connector_source_providers()
    assert any(item["source_id"] == "corp-hub" for item in providers)
    reset_connector_source_manager()


@pytest.mark.anyio
async def test_enterprise_catalogue_dual_version_equal_and_uninstalled(
    tmp_path: Path, monkeypatch
) -> None:
    """Installed equal versions → has_update False; not installed → no installed_version."""
    from jiuwenswarm.extensions.sdk.connector_source import (
        ConnectorCandidate,
        ConnectorSearchResult,
        ConnectorSourceConfig,
        ConnectorSourceProvider,
    )
    from jiuwenswarm.server.runtime.connector import (
        get_connector_source_manager,
        reset_connector_source_manager,
    )
    from jiuwenswarm.server.runtime.marketplace.hub_install_state import HubInstallRecord

    class FakeProvider(ConnectorSourceProvider):
        source_id = "corp-hub"
        provider_type = "openjiuwen-hub"
        display_name = "Corp Hub"
        capabilities = frozenset({"search", "get_detail", "get_artifact"})

        async def search(self, request, context):
            del request, context
            return ConnectorSearchResult(
                items=(
                    ConnectorCandidate(
                        source_id="corp-hub",
                        asset_id="same-asset",
                        package_name="same-mcp",
                        display_name="Same MCP",
                        version_id="1.0.0",
                    ),
                    ConnectorCandidate(
                        source_id="corp-hub",
                        asset_id="fresh-asset",
                        package_name="fresh-mcp",
                        display_name="Fresh MCP",
                        version_id="3.0.0",
                    ),
                ),
                total=2,
                next_page=None,
            )

        async def get_artifact(self, connector_ref, version_id, context):
            raise NotImplementedError

    monkeypatch.setattr(marketplace, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(marketplace.registry, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(marketplace, "_is_enterprise_edition", lambda: True)
    monkeypatch.setattr(marketplace.registry, "list_marketplace_mcps", lambda mcp_filter="builtin": [])
    reset_connector_source_manager()
    runtime = get_connector_source_manager()
    runtime.register_provider(
        ConnectorSourceConfig(
            source_id="corp-hub",
            provider_type="openjiuwen-hub",
            enabled=True,
            capabilities=frozenset({"search", "get_detail", "get_artifact"}),
        ),
        FakeProvider(),
        display_name="Corp Hub",
    )
    marketplace._state().upsert(
        HubInstallRecord(
            asset_id="same-asset",
            package_id="same-mcp",
            kind="mcp",
            version="1.0.0",
            checksum_sha256="abc",
            installed_at="2026-01-01T00:00:00+00:00",
            source_id="corp-hub",
        )
    )

    cards = await marketplace.list_mcps_with_hub("hub", source_id="corp-hub")
    by_id = {card["id"]: card for card in cards}
    same = by_id["same-asset"]
    assert same["installed"] is True
    assert same["version"] == "1.0.0"
    assert same["installed_version"] == "1.0.0"
    assert same["has_update"] is False

    fresh = by_id["fresh-asset"]
    assert fresh.get("installed") is not True
    assert fresh["version"] == "3.0.0"
    assert "installed_version" not in fresh
    assert "has_update" not in fresh
    reset_connector_source_manager()


@pytest.mark.anyio
async def test_personal_list_cards_omit_forced_dual_version_fields(
    tmp_path: Path, monkeypatch
) -> None:
    """Personal HubAssetPort path must not require installed_version / has_update."""
    monkeypatch.setattr(marketplace, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(marketplace.registry, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(marketplace, "_is_enterprise_edition", lambda: False)
    monkeypatch.setattr(
        marketplace.registry,
        "list_marketplace_mcps",
        lambda mcp_filter="builtin": [
            {
                "name": "builtin-mcp",
                "display_name": "Builtin",
                "description": "",
                "category": "",
                "source": "built_in",
                "connection_state": "disconnected",
                "integration_type": "stdio-mcp",
                "has_bundled_skills": False,
            }
        ]
        if mcp_filter == "builtin"
        else [],
    )

    class _EmptyHub:
        async def search_mcp(self, **kwargs):
            del kwargs
            return HubSearchPage(items=(), total=0, page=1, page_size=100)

    cards = await marketplace.list_mcps_with_hub("builtin", hub_port=_EmptyHub())
    assert any(card["name"] == "builtin-mcp" for card in cards)
    for card in cards:
        assert "installed_version" not in card
        assert "has_update" not in card


@pytest.mark.anyio
async def test_personal_default_hub_skips_spi_provider(
    tmp_path: Path, monkeypatch
) -> None:
    """Personal edition keeps default Hub on HubAssetPort (prefer_cache) even if SPI is bound."""
    from jiuwenswarm.extensions.sdk.connector_source import (
        ConnectorCandidate,
        ConnectorSearchResult,
        ConnectorSourceConfig,
        ConnectorSourceProvider,
    )
    from jiuwenswarm.server.runtime.connector import (
        get_connector_source_manager,
        reset_connector_source_manager,
    )

    class SpyProvider(ConnectorSourceProvider):
        source_id = "openjiuwen-hub"
        provider_type = "openjiuwen-hub"
        display_name = "Hub"
        capabilities = frozenset({"search", "get_detail", "get_artifact"})
        searches = 0

        async def search(self, request, context):
            del request, context
            type(self).searches += 1
            return ConnectorSearchResult(
                items=(
                    ConnectorCandidate(
                        source_id="openjiuwen-hub",
                        asset_id="spi-only",
                        package_name="spi-mcp",
                        display_name="SPI",
                        version_id="1.0.0",
                    ),
                ),
                total=1,
                next_page=None,
            )

        async def get_artifact(self, connector_ref, version_id, context):
            raise NotImplementedError

    monkeypatch.setattr(marketplace, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(marketplace.registry, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(
        marketplace.registry, "list_marketplace_mcps", lambda mcp_filter="builtin": []
    )
    monkeypatch.setattr(marketplace, "_is_enterprise_edition", lambda: False)

    hub = FakeHub()
    cache_ports: list[object] = []

    async def fake_cached_asset_catalog(port, kind, *, refresh=False, preload=False):
        del kind, refresh, preload
        cache_ports.append(port)
        return [], {"refreshing": False}

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.marketplace.hub_catalog_cache.cached_asset_catalog",
        fake_cached_asset_catalog,
    )

    SpyProvider.searches = 0
    reset_connector_source_manager()
    get_connector_source_manager().register_provider(
        ConnectorSourceConfig(
            source_id="openjiuwen-hub",
            provider_type="openjiuwen-hub",
            enabled=True,
            capabilities=frozenset({"search", "get_detail", "get_artifact"}),
        ),
        SpyProvider(),
    )

    await marketplace.list_mcps_with_hub(hub_port=hub, cache_mode="prefer_cache")
    assert SpyProvider.searches == 0
    assert cache_ports == [hub]

    # Without hub_port, personal still skips SPI and uses default Hub + cache.
    cache_ports.clear()
    await marketplace.list_mcps_with_hub(cache_mode="prefer_cache")
    assert SpyProvider.searches == 0
    assert len(cache_ports) == 1
    reset_connector_source_manager()


@pytest.mark.anyio
async def test_unknown_source_id_rejected_without_hub_port(
    tmp_path: Path, monkeypatch
) -> None:
    from jiuwenswarm.server.runtime.connector import reset_connector_source_manager

    monkeypatch.setattr(marketplace, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(marketplace, "_is_enterprise_edition", lambda: True)
    reset_connector_source_manager()
    with pytest.raises(ValueError, match="not registered"):
        await marketplace.list_mcps_with_hub("hub", source_id="missing-source")


@pytest.mark.anyio
async def test_enterprise_catalogue_requires_source_id(
    tmp_path: Path, monkeypatch
) -> None:
    """Enterprise hub catalogue requires source_id."""
    monkeypatch.setattr(marketplace, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(marketplace, "_is_enterprise_edition", lambda: True)
    with pytest.raises(ValueError, match="source_id is required"):
        await marketplace.list_mcps_with_hub("hub")


@pytest.mark.anyio
async def test_enterprise_local_list_skips_source_id(
    tmp_path: Path, monkeypatch
) -> None:
    """Enterprise mine tab (filter=local) does not require source_id."""
    monkeypatch.setattr(marketplace, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(marketplace.registry, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(marketplace, "_is_enterprise_edition", lambda: True)
    monkeypatch.setattr(
        marketplace.registry, "list_marketplace_mcps", lambda mcp_filter="builtin": []
    )
    cards = await marketplace.list_mcps_with_hub("local")
    assert cards == []


@pytest.mark.anyio
async def test_enterprise_rejects_unknown_filter(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(marketplace, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(marketplace, "_is_enterprise_edition", lambda: True)
    with pytest.raises(ValueError, match="filter must be hub or local"):
        await marketplace.list_mcps_with_hub("mine", source_id="corp-hub")
    with pytest.raises(ValueError, match="filter must be hub or local"):
        await marketplace.list_mcps_with_hub("builtin", source_id="corp-hub")
    with pytest.raises(ValueError, match="filter must be hub or local"):
        await marketplace.list_mcps_with_hub(source_id="corp-hub")


def _corp_mcp_zip_bytes(
    *,
    package_id: str = "corp-mcp",
    version: str = "2.0.0",
    flat: bool = False,
) -> bytes:
    import io
    import zipfile

    prefix = "" if flat else f"{package_id}/"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            f"{prefix}mcp.json",
            json.dumps({"mcpServers": {package_id: {"url": "https://example.test/mcp"}}}),
        )
        zf.writestr(
            f"{prefix}manifest.json",
            json.dumps(
                {
                    "version": version,
                    "package_type": "mcp",
                    "id": package_id,
                    "name": "Corp MCP",
                    "description": "from corp",
                    "display_name": {"zh": "Corp MCP", "en": "Corp MCP"},
                    "display_description": {"zh": "from corp", "en": "from corp"},
                    "source": "hub",
                    "integration": {"type": "remote-mcp", "file": "mcp.json"},
                }
            ),
        )
    return buf.getvalue()


@pytest.mark.anyio
async def test_enterprise_install_uses_caller_version_without_get_detail(
    tmp_path: Path, monkeypatch
) -> None:
    """Enterprise SPI install takes version_id from caller; no get_detail."""
    from jiuwenswarm.extensions.sdk.connector_source import (
        ConnectorArtifactDescriptor,
            ConnectorSearchResult,
        ConnectorSourceConfig,
        ConnectorSourceProvider,
    )
    from jiuwenswarm.server.runtime.connector import (
        get_connector_source_manager,
        reset_connector_source_manager,
    )

    zip_bytes = _corp_mcp_zip_bytes()

    class InstallProvider(ConnectorSourceProvider):
        source_id = "corp-hub"
        provider_type = "corp-hub"
        display_name = "Corp Hub"
        capabilities = frozenset({"search", "get_artifact"})
        download_allowed_hosts = ("artifacts.example.test",)
        detail_calls = 0
        artifact_calls = 0

        async def search(self, request, context):
            del request, context
            return ConnectorSearchResult(items=(), total=0)

        async def get_detail(self, connector_ref, version_id, context):
            type(self).detail_calls += 1
            return await ConnectorSourceProvider.get_detail(self, connector_ref, version_id, context)

        async def get_artifact(self, connector_ref, version_id, context):
            del context
            type(self).artifact_calls += 1
            assert version_id == "2.0.0"
            return ConnectorArtifactDescriptor(
                source_id=connector_ref.source_id,
                asset_id=connector_ref.asset_id,
                version_id=version_id,
                download_url="https://artifacts.example.test/corp-mcp.zip",
                package_name="corp-mcp",
            )

        async def download_artifact(self, descriptor, download_policy=None):
            del descriptor, download_policy
            return zip_bytes

        async def verify_artifact(self, descriptor, content):
            del descriptor, content
            return None

    monkeypatch.setattr(marketplace, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(marketplace.registry, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(marketplace, "_is_enterprise_edition", lambda: True)
    reset_connector_source_manager()
    runtime = get_connector_source_manager()
    provider = InstallProvider()
    runtime.register_provider(
        ConnectorSourceConfig(
            source_id="corp-hub",
            provider_type="corp-hub",
            enabled=True,
            capabilities=frozenset({"search", "get_artifact"}),
        ),
        provider,
        display_name="Corp Hub",
    )

    with pytest.raises(ValueError, match="version_id is required"):
        await marketplace.install_hub_mcp("corp-asset", source_id="corp-hub")

    installed = await marketplace.install_hub_mcp(
        "corp-asset",
        source_id="corp-hub",
        version_id="2.0.0",
        package_name="corp-mcp",
    )
    assert installed == {
        "id": "corp-asset",
        "name": "corp-mcp",
        "installed": True,
        "source": "hub",
        "source_id": "corp-hub",
    }
    assert InstallProvider.detail_calls == 0
    assert InstallProvider.artifact_calls == 1
    assert (tmp_path / "mcp" / "mcp_hub" / "corp-mcp" / "manifest.json").is_file()
    reset_connector_source_manager()


@pytest.mark.anyio
async def test_enterprise_install_accepts_flat_hub_zip(
    tmp_path: Path, monkeypatch
) -> None:
    """Real Hub MCP zips are often flat; stage under package_id before validate."""
    from jiuwenswarm.extensions.sdk.connector_source import (
        ConnectorArtifactDescriptor,
            ConnectorSearchResult,
        ConnectorSourceConfig,
        ConnectorSourceProvider,
    )
    from jiuwenswarm.server.runtime.connector import (
        get_connector_source_manager,
        reset_connector_source_manager,
    )

    zip_bytes = _corp_mcp_zip_bytes(package_id="harmonyos-mcp", version="1.0.0", flat=True)

    class FlatZipProvider(ConnectorSourceProvider):
        source_id = "corp-hub"
        provider_type = "corp-hub"
        display_name = "Corp Hub"
        capabilities = frozenset({"search", "get_artifact"})
        download_allowed_hosts = ("artifacts.example.test",)

        async def search(self, request, context):
            del request, context
            return ConnectorSearchResult(items=(), total=0)

        async def get_artifact(self, connector_ref, version_id, context):
            del context
            return ConnectorArtifactDescriptor(
                source_id=connector_ref.source_id,
                asset_id=connector_ref.asset_id,
                version_id=version_id,
                download_url="https://artifacts.example.test/flat.zip",
                package_name="harmonyos-mcp",
            )

        async def download_artifact(self, descriptor, download_policy=None):
            del descriptor, download_policy
            return zip_bytes

        async def verify_artifact(self, descriptor, content):
            del descriptor, content
            return None

    monkeypatch.setattr(marketplace, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(marketplace.registry, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(marketplace, "_is_enterprise_edition", lambda: True)
    reset_connector_source_manager()
    runtime = get_connector_source_manager()
    runtime.register_provider(
        ConnectorSourceConfig(
            source_id="corp-hub",
            provider_type="corp-hub",
            enabled=True,
            capabilities=frozenset({"search", "get_artifact"}),
        ),
        FlatZipProvider(),
        display_name="Corp Hub",
    )

    installed = await marketplace.install_hub_mcp(
        "527993ffd3aa4fc2bd67727a056b9d59",
        source_id="corp-hub",
        version_id="1.0.0",
        package_name="harmonyos-mcp",
    )
    assert installed["name"] == "harmonyos-mcp"
    assert (
        tmp_path / "mcp" / "mcp_hub" / "harmonyos-mcp" / "manifest.json"
    ).is_file()
    reset_connector_source_manager()
