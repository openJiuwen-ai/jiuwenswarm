# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from jiuwenswarm.server.runtime import extension_package_manager as catalog
from jiuwenswarm.server.runtime.marketplace.hub_asset_port import (
    HubAssetDetail,
    HubAssetSummary,
    HubResolvedDownload,
    HubSearchPage,
)
from jiuwenswarm.server.runtime.marketplace.hub_install_state import (
    HubInstallRecord,
    HubInstallStateStore,
)


def _item(
    asset_id: str = "sales-expert",
    kind: str = "agent_template",
    package_name: str | None = None,
) -> HubAssetSummary:
    return HubAssetSummary(
        kind=kind,
        asset_id=asset_id,
        display_name="销售专家" if kind == "agent_template" else "销售插件",
        short_description="分析销售数据",
        public_latest_version="1.2.3",
        icon_uri="https://example.test/icon.png",
        tags=("sales", "analysis"),
        package_name=package_name or asset_id,
    )


class FakeHubAssetPort:
    def __init__(self, item: HubAssetSummary | None = None) -> None:
        self.item = item or _item()
        self.list_calls: list[str] = []
        self.artifact_calls = 0

    async def search_assets(self, request) -> HubSearchPage:
        self.list_calls.append(request.kind)
        return HubSearchPage(
            items=(self.item,), total=1, page=request.page, page_size=100
        )

    async def query_asset(self, request) -> HubAssetDetail:
        assert request.asset_id == self.item.asset_id
        version = self.item.public_latest_version
        if not version:
            raise ValueError(f"Hub package has no public version: {request.asset_id}")
        return HubAssetDetail(
            kind=self.item.kind,
            asset_id=request.asset_id,
            version=version,
            display_name=self.item.display_name,
            short_description=self.item.short_description,
            detail_description="完整的远端专家详情",
            icon_uri=self.item.icon_uri,
            tags=self.item.tags,
            package_name=self.item.package_name,
        )

    async def resolve_download(self, request) -> HubResolvedDownload:
        assert request.asset_id == self.item.asset_id
        assert request.version == "1.2.3"
        self.artifact_calls += 1
        return HubResolvedDownload(
            kind=self.item.kind,
            asset_id=request.asset_id,
            version="1.2.3",
            download_url="https://downloads.example.test/package.zip",
            checksum_sha256="a" * 64,
            package_name=self.item.package_name,
        )


class PortOnlyHub:
    def __init__(self) -> None:
        self.requests: list[object] = []

    async def search_assets(self, request) -> HubSearchPage:
        self.requests.append(request)
        item = _item(kind="agent_template")
        return HubSearchPage(
            items=(item,), total=1, page=request.page, page_size=100
        )

    async def query_asset(self, request) -> HubAssetDetail:
        raise AssertionError("list must not query details")

    async def resolve_download(self, request) -> HubResolvedDownload:
        raise AssertionError("list must not resolve downloads")


class FakeDownloader:
    def __init__(self, *, connectors: tuple[str, ...] = ()) -> None:
        self.calls = 0
        self.connectors = connectors

    async def download_and_extract(
        self, artifact: HubResolvedDownload, destination: Path
    ) -> None:
        self.calls += 1
        package_root = destination / artifact.package_name
        package_root.mkdir(parents=True)
        package_type = artifact.kind
        manifest = {
            "package_type": package_type,
            "version": artifact.version,
            "display_name": {"zh": "销售专家", "en": "Sales Expert"},
            "display_description": {"zh": "分析销售数据", "en": "Analyze sales"},
            "mcps": [{"connector": name} for name in self.connectors],
        }
        if package_type == "agent_template":
            manifest["name"] = artifact.package_name
        else:
            manifest["id"] = artifact.package_name
        (package_root / "manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )


class CamelCaseManifestDownloader(FakeDownloader):
    async def download_and_extract(
        self, artifact: HubResolvedDownload, destination: Path
    ) -> None:
        self.calls += 1
        package_root = destination / artifact.package_name
        package_root.mkdir(parents=True)
        manifest = {
            "packageType": artifact.kind,
            "version": artifact.version,
            "displayName": {"zh": "销售资产", "en": "Sales Asset"},
            "displayDescription": {"zh": "分析销售数据", "en": "Analyze sales"},
            "defaultInitInput": {"zh": "分析数据", "en": "Analyze data"},
            "quickInputs": [{"zh": "生成报告", "en": "Create report"}],
        }
        if artifact.kind == "agent_template":
            manifest["agentCard"] = {
                "id": artifact.package_name,
                "name": "Sales Expert",
                "description": "Analyze sales data",
            }
        else:
            manifest["id"] = artifact.package_name
        (package_root / "manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )


class PagingHubAssetPort(FakeHubAssetPort):
    async def search_assets(self, request) -> HubSearchPage:
        self.list_calls.append(f"{request.kind}:{request.page}:{request.page_size}")
        item = _item(f"remote-{request.page}", "agent_template")
        return HubSearchPage(
            items=(item,), total=2, page=request.page, page_size=1
        )


class FailingListHubAssetPort(FakeHubAssetPort):
    async def search_assets(self, request) -> HubSearchPage:
        self.list_calls.append(request.kind)
        raise RuntimeError("hub unavailable")


class WrongKindHubAssetPort(FakeHubAssetPort):
    async def query_asset(self, request) -> HubAssetDetail:
        detail = await super().query_asset(request)
        return replace(detail, kind="plugin")


class WrongQueryIdHubAssetPort(FakeHubAssetPort):
    async def query_asset(self, request) -> HubAssetDetail:
        detail = await super().query_asset(request)
        return replace(detail, asset_id="different-expert")


class WrongArtifactIdHubAssetPort(FakeHubAssetPort):
    async def resolve_download(self, request) -> HubResolvedDownload:
        artifact = await super().resolve_download(request)
        return replace(artifact, asset_id="different-expert")


class WrongArtifactVersionHubAssetPort(FakeHubAssetPort):
    async def resolve_download(self, request) -> HubResolvedDownload:
        artifact = await super().resolve_download(request)
        return replace(artifact, version="9.9.9")


class WrongIdDownloader(FakeDownloader):
    async def download_and_extract(
        self, artifact: HubResolvedDownload, destination: Path
    ) -> None:
        await super().download_and_extract(artifact, destination)
        manifest_path = destination / artifact.package_name / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["name"] = "different-expert"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


@pytest.mark.asyncio
async def test_equipment_catalog_depends_only_on_hub_asset_port(
    extension_workspace: Path,
) -> None:
    hub = PortOnlyHub()

    cards = await catalog.list_agent_templates_with_hub({}, hub_port=hub)

    assert [card["id"] for card in cards] == ["sales-expert"]
    assert len(hub.requests) == 1
    assert hub.requests[0].kind == "agent_template"


@pytest.mark.asyncio
async def test_install_rejects_wrong_kind_port_result_before_download(
    extension_workspace: Path,
) -> None:
    downloader = FakeDownloader()

    with pytest.raises(ValueError, match="package type mismatch"):
        await catalog.install_equipment_from_hub_gated(
            "agent_templates",
            {"id": "sales-expert"},
            hub_port=WrongKindHubAssetPort(),
            downloader=downloader,
        )

    assert downloader.calls == 0


@pytest.mark.asyncio
async def test_show_rejects_wrong_asset_id_port_result(
    extension_workspace: Path,
) -> None:
    with pytest.raises(ValueError, match="asset id mismatch"):
        await catalog.show_agent_template_with_hub(
            "sales-expert", hub_port=WrongQueryIdHubAssetPort()
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("hub_port", "message"),
    [
        (WrongQueryIdHubAssetPort(), "asset id mismatch"),
        (WrongArtifactIdHubAssetPort(), "artifact asset id mismatch"),
        (WrongArtifactVersionHubAssetPort(), "artifact version mismatch"),
    ],
)
async def test_install_rejects_wrong_artifact_identity_before_download(
    extension_workspace: Path,
    hub_port: FakeHubAssetPort,
    message: str,
) -> None:
    downloader = FakeDownloader()

    with pytest.raises(ValueError, match=message):
        await catalog.install_equipment_from_hub_gated(
            "agent_templates",
            {"id": "sales-expert"},
            hub_port=hub_port,
            downloader=downloader,
        )

    assert downloader.calls == 0


def test_hub_install_state_migrates_legacy_plugin_type_to_stable_kind(
    extension_workspace: Path,
) -> None:
    kind_root = extension_workspace / "plugins" / "agent_templates"
    kind_root.mkdir(parents=True)
    (kind_root / "hub_state.json").write_text(
        json.dumps(
            {
                "packages": [
                    {
                        "asset_id": "sales-expert",
                        "plugin_type": "agent-template",
                        "version": "1.2.3",
                        "checksum_sha256": "a" * 64,
                        "installed_at": "2026-09-01T00:00:00Z",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    record = HubInstallStateStore(kind_root).get("sales-expert")

    assert record is not None
    assert record.kind == "agent_template"
    assert record.package_id == "sales-expert"


def test_hub_install_state_indexes_asset_uuid_and_runtime_package_id(
    extension_workspace: Path,
) -> None:
    kind_root = extension_workspace / "plugins" / "agent_templates"
    store = HubInstallStateStore(kind_root)
    store.upsert(
        HubInstallRecord(
            asset_id="482becff9f044ba9bad9caef2e43b539",
            package_id="sales-expert",
            kind="agent_template",
            version="1.2.3",
            checksum_sha256="a" * 64,
            installed_at="2026-09-01T00:00:00Z",
        )
    )

    assert store.get("482becff9f044ba9bad9caef2e43b539").package_id == (
        "sales-expert"
    )
    assert store.get_by_package_id("sales-expert").asset_id == (
        "482becff9f044ba9bad9caef2e43b539"
    )


@pytest.mark.asyncio
async def test_list_merges_remote_card_without_writing_disk(
    extension_workspace: Path,
) -> None:
    hub = FakeHubAssetPort()

    cards = await catalog.list_agent_templates_with_hub({}, hub_port=hub)

    assert [(card["id"], card["source"], card["installed"]) for card in cards] == [
        ("sales-expert", "hub", False)
    ]
    assert cards[0]["displayName"] == {"zh": "销售专家", "en": "销售专家"}
    assert not (
        extension_workspace / "plugins" / "agent_templates" / "local" / "sales-expert"
    ).exists()


@pytest.mark.asyncio
async def test_remote_list_does_not_create_equipment_directories(
    extension_workspace: Path,
) -> None:
    kind_root = extension_workspace / "plugins" / "agent_templates"

    cards = await catalog.list_agent_templates_with_hub(
        {"filter": "builtin+hub"}, hub_port=FakeHubAssetPort()
    )

    assert [card["id"] for card in cards] == ["sales-expert"]
    assert not kind_root.exists()


@pytest.mark.asyncio
async def test_list_fetches_all_hub_pages(extension_workspace: Path) -> None:
    hub = PagingHubAssetPort()

    cards = await catalog.list_agent_templates_with_hub({}, hub_port=hub)

    assert [card["id"] for card in cards] == ["remote-1", "remote-2"]
    assert hub.list_calls == ["agent_template:1:100", "agent_template:2:100"]


@pytest.mark.asyncio
async def test_list_hub_failure_keeps_local_catalog(
    extension_workspace: Path,
) -> None:
    local = extension_workspace / "plugins" / "agent_templates" / "local" / "local-one"
    local.mkdir(parents=True)
    (local / "manifest.json").write_text(
        json.dumps({"package_type": "agent_template", "name": "local-one"}),
        encoding="utf-8",
    )
    catalog.upsert_agent_template_marketplace_entry(
        "local-one", installed=True, source="local"
    )

    cards = await catalog.list_agent_templates_with_hub(
        {}, hub_port=FailingListHubAssetPort()
    )

    assert [(card["id"], card["source"], card["installed"]) for card in cards] == [
        ("local-one", "local", True)
    ]


@pytest.mark.asyncio
async def test_same_named_local_package_wins_without_hub_provenance(
    extension_workspace: Path,
) -> None:
    local = (
        extension_workspace / "plugins" / "agent_templates" / "local" / "sales-expert"
    )
    local.mkdir(parents=True)
    (local / "manifest.json").write_text(
        json.dumps({"package_type": "agent_template", "name": "sales-expert"}),
        encoding="utf-8",
    )
    catalog.upsert_agent_template_marketplace_entry(
        "sales-expert", installed=True, source="local"
    )

    cards = await catalog.list_agent_templates_with_hub({}, hub_port=FakeHubAssetPort())

    assert [(card["id"], card["source"], card["installed"]) for card in cards] == [
        ("sales-expert", "local", True)
    ]


@pytest.mark.asyncio
async def test_list_uses_provenance_to_return_installed_hub_package_once(
    extension_workspace: Path,
) -> None:
    local = (
        extension_workspace / "plugins" / "agent_templates" / "local" / "sales-expert"
    )
    local.mkdir(parents=True)
    (local / "manifest.json").write_text(
        json.dumps({"package_type": "agent_template", "name": "sales-expert"}),
        encoding="utf-8",
    )
    catalog.upsert_agent_template_marketplace_entry(
        "sales-expert", installed=True, source="hub"
    )
    HubInstallStateStore(local.parent.parent).upsert(
        HubInstallRecord(
            asset_id="sales-expert",
            kind="agent_template",
            version="1.2.3",
            checksum_sha256="a" * 64,
            installed_at="2026-09-01T00:00:00Z",
        )
    )

    cards = await catalog.list_agent_templates_with_hub({}, hub_port=FakeHubAssetPort())

    assert [(card["id"], card["source"], card["installed"]) for card in cards] == [
        ("sales-expert", "hub", True)
    ]


@pytest.mark.asyncio
async def test_mine_list_is_local_only_and_does_not_wait_for_hub(
    extension_workspace: Path,
) -> None:
    local = (
        extension_workspace / "plugins" / "agent_templates" / "local" / "sales-expert"
    )
    local.mkdir(parents=True)
    (local / "manifest.json").write_text(
        json.dumps({"package_type": "agent_template", "name": "sales-expert"}),
        encoding="utf-8",
    )
    catalog.upsert_agent_template_marketplace_entry(
        "sales-expert", installed=True, source="hub"
    )
    HubInstallStateStore(local.parent.parent).upsert(
        HubInstallRecord(
            asset_id="sales-expert",
            kind="agent_template",
            version="1.2.3",
            checksum_sha256="a" * 64,
            installed_at="2026-09-01T00:00:00Z",
        )
    )
    hub = FakeHubAssetPort()

    cards = await catalog.list_agent_templates_with_hub(
        {"filter": "mine"}, hub_port=hub
    )

    assert [(card["id"], card["source"], card["installed"]) for card in cards] == [
        ("sales-expert", "hub", True)
    ]
    assert hub.list_calls == []


@pytest.mark.asyncio
async def test_remote_show_has_metadata_but_no_local_capabilities(
    extension_workspace: Path,
) -> None:
    card = await catalog.show_agent_template_with_hub(
        "sales-expert", hub_port=FakeHubAssetPort()
    )

    assert card is not None
    assert card["source"] == "hub"
    assert card["version"] == "1.2.3"
    assert card["details"] == "完整的远端专家详情"
    assert card["skills"] == []
    assert card["tools"] == []
    assert card["rails"] == []
    assert card["mcps"] == []
    assert card["pending_connectors"] == []


@pytest.mark.asyncio
async def test_unpublished_hub_asset_cannot_be_shown_or_installed(
    extension_workspace: Path,
) -> None:
    hub = FakeHubAssetPort(
        replace(_item(), public_latest_version="")
    )
    downloader = FakeDownloader()

    with pytest.raises(ValueError, match="no public version"):
        await catalog.show_agent_template_with_hub("sales-expert", hub_port=hub)
    with pytest.raises(ValueError, match="no public version"):
        await catalog.install_equipment_from_hub_gated(
            "agent_templates",
            {"id": "sales-expert"},
            hub_port=hub,
            downloader=downloader,
        )

    assert downloader.calls == 0


@pytest.mark.asyncio
async def test_plugin_package_uses_symmetric_hub_list_show_and_install(
    extension_workspace: Path,
) -> None:
    asset_id = "plugin-asset-uuid"
    hub = FakeHubAssetPort(
        _item(asset_id, "plugin", package_name="sales-plugin")
    )
    downloader = FakeDownloader()

    cards = await catalog.list_plugin_packages_with_hub({}, hub_port=hub)
    detail = await catalog.show_plugin_package_with_hub(asset_id, hub_port=hub)
    ok, payload = await catalog.install_equipment_from_hub_gated(
        "plugin_packages",
        {"id": asset_id},
        hub_port=hub,
        downloader=downloader,
    )

    assert [(card["id"], card["packageName"], card["source"]) for card in cards] == [
        (asset_id, "sales-plugin", "hub")
    ]
    assert detail is not None and detail["source"] == "hub"
    assert (ok, payload) == (True, {})
    assert catalog.is_plugin_allowed("sales-plugin") is True

    catalog.uninstall_plugin_package({"id": asset_id})

    assert catalog.is_plugin_allowed("sales-plugin") is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "collection", "asset_id", "package_name"),
    [
        ("agent_template", "agent_templates", "expert-asset-uuid", "sales-expert"),
        ("plugin", "plugin_packages", "plugin-asset-uuid", "sales-plugin"),
    ],
)
async def test_hub_install_normalizes_camel_case_manifest(
    extension_workspace: Path,
    kind: str,
    collection: str,
    asset_id: str,
    package_name: str,
) -> None:
    hub = FakeHubAssetPort(_item(asset_id, kind, package_name=package_name))

    ok, payload = await catalog.install_equipment_from_hub_gated(
        collection,
        {"id": asset_id},
        hub_port=hub,
        downloader=CamelCaseManifestDownloader(),
    )

    assert (ok, payload) == (True, {})
    package_root = extension_workspace / "plugins" / collection / "local" / package_name
    manifest = json.loads((package_root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["package_type"] == kind
    assert manifest["display_name"] == {"zh": "销售资产", "en": "Sales Asset"}
    assert manifest["display_description"] == {
        "zh": "分析销售数据",
        "en": "Analyze sales",
    }
    assert manifest["default_init_input"] == {
        "zh": "分析数据",
        "en": "Analyze data",
    }
    assert manifest["quick_inputs"] == [{"zh": "生成报告", "en": "Create report"}]
    expected_id_field = "name" if kind == "agent_template" else "id"
    assert manifest[expected_id_field] == package_name


@pytest.mark.asyncio
async def test_install_keeps_prepared_package_when_connectors_pending_and_reuses_it(
    monkeypatch: pytest.MonkeyPatch, extension_workspace: Path
) -> None:
    hub = FakeHubAssetPort()
    downloader = FakeDownloader(connectors=("crm",))
    state = {"crm": "disconnected"}
    monkeypatch.setattr(catalog, "get_mcp_record", lambda name: {"state": state[name]})

    ok, payload = await catalog.install_equipment_from_hub_gated(
        "agent_templates",
        {"id": "sales-expert"},
        hub_port=hub,
        downloader=downloader,
    )

    assert ok is False
    assert payload["pending_connectors"] == ["crm"]
    assert downloader.calls == 1
    local = (
        extension_workspace / "plugins" / "agent_templates" / "local" / "sales-expert"
    )
    assert local.is_dir()
    market = {
        entry["id"]: entry
        for entry in catalog.read_agent_template_marketplace_entries()
    }
    assert market["sales-expert"] == {
        "id": "sales-expert",
        "source": "hub",
        "installed": False,
    }

    state["crm"] = "connected"
    ok, payload = await catalog.install_equipment_from_hub_gated(
        "agent_templates",
        {"id": "sales-expert"},
        hub_port=hub,
        downloader=downloader,
    )

    assert (ok, payload) == (True, {})
    assert downloader.calls == 1
    assert hub.artifact_calls == 1
    market = {
        entry["id"]: entry
        for entry in catalog.read_agent_template_marketplace_entries()
    }
    assert market["sales-expert"]["installed"] is True
    assert market["sales-expert"]["source"] == "hub"

    catalog.uninstall_agent_template({"id": "sales-expert"})
    cards = await catalog.list_agent_templates_with_hub({}, hub_port=hub)

    assert [(card["id"], card["source"], card["installed"]) for card in cards] == [
        ("sales-expert", "hub", False)
    ]
    assert HubInstallStateStore(local.parent.parent).get("sales-expert") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "asset_id", "package_name", "show"),
    [
        (
            "agent_template",
            "expert-asset-uuid",
            "sales-expert",
            catalog.show_agent_template_with_hub,
        ),
        (
            "plugin",
            "plugin-asset-uuid",
            "sales-plugin",
            catalog.show_plugin_package_with_hub,
        ),
    ],
)
async def test_pending_hub_equipment_show_uses_remote_detail(
    monkeypatch: pytest.MonkeyPatch,
    extension_workspace: Path,
    kind: str,
    asset_id: str,
    package_name: str,
    show,
) -> None:
    hub = FakeHubAssetPort(_item(asset_id, kind, package_name=package_name))
    monkeypatch.setattr(catalog, "get_mcp_record", lambda _name: None)
    collection = "agent_templates" if kind == "agent_template" else "plugin_packages"

    ok, payload = await catalog.install_equipment_from_hub_gated(
        collection,
        {"id": asset_id},
        hub_port=hub,
        downloader=FakeDownloader(connectors=("crm",)),
    )

    assert ok is False and payload["pending_connectors"] == ["crm"]
    for identifier in (asset_id, package_name):
        card = await show(identifier, hub_port=hub)
        assert card is not None
        assert card["id"] == asset_id
        assert card["packageName"] == package_name
        assert card["details"] == "完整的远端专家详情"
        assert card["installed"] is False


@pytest.mark.asyncio
async def test_pending_hub_expert_files_are_previewable_by_uuid_or_package_name(
    monkeypatch: pytest.MonkeyPatch, extension_workspace: Path
) -> None:
    asset_id = "expert-asset-uuid"
    package_name = "sales-expert"
    hub = FakeHubAssetPort(
        _item(asset_id, "agent_template", package_name=package_name)
    )
    monkeypatch.setattr(catalog, "get_mcp_record", lambda _name: None)
    ok, _payload = await catalog.install_equipment_from_hub_gated(
        "agent_templates",
        {"id": asset_id},
        hub_port=hub,
        downloader=FakeDownloader(connectors=("crm",)),
    )
    assert ok is False

    for identifier in (asset_id, package_name):
        tree = catalog.list_agent_template_files(identifier)
        assert any(entry["path"] == "manifest.json" for entry in tree)
        preview = catalog.read_agent_template_file(identifier, "manifest.json")
        assert preview["path"] == "manifest.json"


@pytest.mark.asyncio
async def test_installed_hub_expert_files_resolve_by_uuid_and_package_name(
    extension_workspace: Path,
) -> None:
    asset_id = "expert-asset-uuid"
    package_name = "sales-expert"
    hub = FakeHubAssetPort(
        _item(asset_id, "agent_template", package_name=package_name)
    )
    ok, payload = await catalog.install_equipment_from_hub_gated(
        "agent_templates",
        {"id": asset_id},
        hub_port=hub,
        downloader=FakeDownloader(),
    )
    assert (ok, payload) == (True, {})

    for identifier in (asset_id, package_name):
        tree = catalog.list_agent_template_files(identifier)
        assert any(entry["path"] == "manifest.json" for entry in tree)
        preview = catalog.read_agent_template_file(identifier, "manifest.json")
        assert preview["path"] == "manifest.json"


def test_hub_expert_files_preview_without_marketplace_entry(
    extension_workspace: Path,
) -> None:
    asset_id = "expert-asset-uuid"
    package_name = "sales-expert"
    package = (
        extension_workspace
        / "plugins"
        / "agent_templates"
        / "local"
        / package_name
    )
    package.mkdir(parents=True)
    (package / "manifest.json").write_text(
        json.dumps({"package_type": "agent_template", "name": package_name}),
        encoding="utf-8",
    )
    HubInstallStateStore(package.parent.parent).upsert(
        HubInstallRecord(
            asset_id=asset_id,
            package_id=package_name,
            kind="agent_template",
            version="1.0.0",
            checksum_sha256="a" * 64,
            installed_at="2026-09-02T00:00:00Z",
        )
    )

    for identifier in (asset_id, package_name):
        tree = catalog.list_agent_template_files(identifier)
        assert any(entry["path"] == "manifest.json" for entry in tree)
        preview = catalog.read_agent_template_file(identifier, "manifest.json")
        assert preview["path"] == "manifest.json"


@pytest.mark.asyncio
async def test_invalid_hub_manifest_leaves_no_partial_package_or_state(
    extension_workspace: Path,
) -> None:
    local = (
        extension_workspace / "plugins" / "agent_templates" / "local" / "sales-expert"
    )

    with pytest.raises(ValueError, match="package id mismatch"):
        await catalog.install_equipment_from_hub_gated(
            "agent_templates",
            {"id": "sales-expert"},
            hub_port=FakeHubAssetPort(),
            downloader=WrongIdDownloader(),
        )

    assert not local.exists()
    assert HubInstallStateStore(local.parent.parent).get("sales-expert") is None
    assert catalog.read_agent_template_marketplace_entries() == []


@pytest.mark.asyncio
async def test_marketplace_write_failure_removes_prepared_hub_package(
    monkeypatch: pytest.MonkeyPatch, extension_workspace: Path
) -> None:
    local = (
        extension_workspace / "plugins" / "agent_templates" / "local" / "sales-expert"
    )

    def fail_marketplace_write(*_args, **_kwargs) -> None:
        raise OSError("marketplace unavailable")

    monkeypatch.setattr(
        catalog, "upsert_agent_template_marketplace_entry", fail_marketplace_write
    )

    with pytest.raises(OSError, match="marketplace unavailable"):
        await catalog.install_equipment_from_hub_gated(
            "agent_templates",
            {"id": "sales-expert"},
            hub_port=FakeHubAssetPort(),
            downloader=FakeDownloader(),
        )

    assert not local.exists()
    assert HubInstallStateStore(local.parent.parent).get("sales-expert") is None


@pytest.mark.asyncio
async def test_hub_asset_uuid_remains_external_while_package_name_drives_runtime(
    extension_workspace: Path,
) -> None:
    asset_id = "482becff9f044ba9bad9caef2e43b539"
    hub = FakeHubAssetPort(
        _item(asset_id, "agent_template", package_name="sales-expert")
    )
    local = (
        extension_workspace / "plugins" / "agent_templates" / "local" / "sales-expert"
    )

    cards = await catalog.list_agent_templates_with_hub({}, hub_port=hub)
    ok, payload = await catalog.install_equipment_from_hub_gated(
        "agent_templates",
        {"id": asset_id},
        hub_port=hub,
        downloader=FakeDownloader(),
    )
    detail = await catalog.show_agent_template_with_hub(asset_id, hub_port=hub)

    assert cards[0]["id"] == asset_id
    assert cards[0]["packageName"] == "sales-expert"
    assert (ok, payload) == (True, {})
    assert local.is_dir()
    assert detail is not None
    assert detail["id"] == asset_id
    assert detail["packageName"] == "sales-expert"
    assert catalog.is_agent_template_installed("sales-expert") is True
    record = HubInstallStateStore(local.parent.parent).get(asset_id)
    assert record is not None and record.package_id == "sales-expert"

    catalog.uninstall_agent_template({"id": asset_id})

    assert not local.exists()
    assert HubInstallStateStore(local.parent.parent).get(asset_id) is None
    cards = await catalog.list_agent_templates_with_hub({}, hub_port=hub)
    assert [(card["id"], card["installed"]) for card in cards] == [
        (asset_id, False)
    ]
