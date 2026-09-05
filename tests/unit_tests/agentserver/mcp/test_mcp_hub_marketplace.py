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
    from jiuwenswarm.gateway.channel_manager.web.app_web_handlers import (
        _FORWARD_NO_LOCAL_HANDLER_METHODS,
        _FORWARD_REQ_METHODS,
    )

    methods = {"mcp.install", "mcp.uninstall"}
    assert methods <= _FORWARD_REQ_METHODS
    assert methods <= _FORWARD_NO_LOCAL_HANDLER_METHODS


class FakeHub:
    async def search_assets(self, request):
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
            "tags": ["tools"],
        }
    ]
    detail = await marketplace.show_mcp_with_hub("mcp-asset-uuid", hub_port=hub)
    assert detail["description"] == "Remote MCP details"
    assert detail["mcp_spec"] is None
    assert detail["skills"] == []
    assert detail["tools"] == []

    installed = await marketplace.install_hub_mcp(
        "mcp-asset-uuid", hub_port=hub, downloader=FakeDownloader()
    )
    assert installed["name"] == "hub-mcp"
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
