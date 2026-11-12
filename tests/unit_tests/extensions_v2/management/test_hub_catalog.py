# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from pathlib import Path
from shutil import copytree
from types import SimpleNamespace

import pytest

from jiuwenswarm.extensions_v2.management import hub_catalog
from jiuwenswarm.extensions_v2.management.catalog import (
    build_state_index,
    entry_from_root,
    find_entry,
    project_detail,
)
from jiuwenswarm.extensions_v2.management.hub_catalog import (
    hub_card,
    hub_detail,
    hub_file_content,
    hub_file_tree,
)
from jiuwenswarm.server.runtime.marketplace.extension_install_state import (
    ExtensionInstallRecord,
    ExtensionInstallState,
)
from jiuwenswarm.server.runtime.marketplace.hub_asset_port import (
    HubAssetDetail,
    HubAssetSummary,
)
from jiuwenswarm.server.runtime.marketplace.hub_asset_type_adapter import (
    get_hub_asset_type_contract,
    resolve_hub_asset_kind,
)
from jiuwenswarm.server.runtime.marketplace.hub_client import HubClient
from jiuwenswarm.server.runtime.marketplace.hub_models import HubVersionDetail

from .support import write_package


def _state() -> SimpleNamespace:
    installed = Path("extension_pkgs") / "webui.login-demo" / "1.0.0"
    return SimpleNamespace(
        active=[SimpleNamespace(package_id="webui.login-demo")],
        installed=[installed],
    )


def test_system_plugin_hub_type_contract() -> None:
    assert resolve_hub_asset_kind("system-plugin", "system-plugin") == "system_plugin"
    contract = get_hub_asset_type_contract("system_plugin")
    assert contract.hub_plugin_type == "system-plugin"
    assert contract.local_package_type == "extension"


def test_hub_card_marks_the_matching_package_installed() -> None:
    card = hub_card(
        HubAssetSummary(
            kind="system_plugin",
            asset_id="asset-1",
            display_name="Auth",
            short_description="Sign in",
            public_latest_version="1.0.0",
            icon_uri="https://example.com/icon.png",
            tags=("login",),
            package_name="webui.login-demo",
        ),
        build_state_index(_state()),
    )
    assert card["id"] == "asset-1"
    assert card["installed"] is True
    assert card["active"] is True
    assert card["source"] == "hub"
    assert card["displayName"] == {"zh": "Auth", "en": "Auth"}


def test_hub_detail_exposes_profile_and_preview() -> None:
    detail = HubAssetDetail(
        kind="system_plugin",
        asset_id="asset-1",
        version="1.0.0",
        display_name="Auth",
        short_description="Sign in",
        detail_description="# Auth",
        icon_uri="https://example.com/icon.png",
        tags=("login",),
        package_name="webui.login-demo",
        preview_url="https://example.com/preview.png",
        versions=("0.9.0", "1.0.0"),
        package_profile={
            "package_type": "extension",
            "domain": "gateway",
            "details": "## Features",
            "events": [{"name": "ui.ready", "description": "Page mounted"}],
        },
    )
    card = hub_detail(detail, _state())
    assert card["source"] == "hub"
    assert card["version"] == "1.0.0"
    assert card["versions"] == ["0.9.0", "1.0.0"]
    assert card["preview"] == "https://example.com/preview.png"
    assert card["details"] == {"zh": "## Features", "en": "## Features"}
    assert card["readme"] == {"zh": "# Auth", "en": "# Auth"}
    assert card["events"] == [
        {"name": "ui.ready", "description": {"zh": "Page mounted", "en": "Page mounted"}}
    ]


def test_version_detail_reads_preview_url() -> None:
    detail = HubVersionDetail.from_payload(
        {
            "asset_id": "asset-1",
            "version": "1.0.0",
            "plugin_type": "system-plugin",
            "asset_type": "system-plugin",
            "name": "webui.login-demo",
            "preview_url": "https://example.com/preview.png",
        }
    )
    assert detail.preview_url == "https://example.com/preview.png"


def test_local_package_detail_is_marked_as_local(tmp_path: Path) -> None:
    root = write_package(tmp_path / "examples", package_id="local.pkg")
    entry = entry_from_root(root)
    assert entry is not None

    detail = project_detail(entry, SimpleNamespace(active=[], installed=[]))

    assert detail["source"] == "local"


def test_find_entry_uses_the_installed_version_when_a_preset_exists(tmp_path: Path) -> None:
    examples = tmp_path / "examples"
    write_package(examples, package_id="webui.demo", version="1.0.0")
    installed = write_package(tmp_path / "staged", package_id="webui.demo", version="1.0.1")
    store_root = tmp_path / "installed" / "webui.demo" / "1.0.1"
    copytree(installed, store_root)
    state = SimpleNamespace(
        active=[
            SimpleNamespace(package_id="webui.demo", version="1.0.1", root=store_root),
        ],
        installed=[store_root],
    )

    entry = find_entry("webui.demo", examples, state, install_dir=tmp_path / "installed")

    assert entry.version == "1.0.1"


def test_install_state_prefers_the_active_record(tmp_path: Path) -> None:
    store = ExtensionInstallState.for_install_dir(tmp_path / "installed")
    store.upsert(
        ExtensionInstallRecord(
            package_id="webui.demo",
            version="1.0.1",
            origin="hub",
            installed_at="2026-01-01T00:00:00+00:00",
        )
    )
    store.upsert(
        ExtensionInstallRecord(
            package_id="webui.demo",
            version="1.0.0",
            origin="hub",
            active=False,
            enabled=False,
            installed_at="2026-01-02T00:00:00+00:00",
        )
    )

    assert store.get("webui.demo").version == "1.0.1"


def test_hub_detail_uses_the_runtime_active_version(tmp_path: Path) -> None:
    installed_root = write_package(
        tmp_path / "installed" / "webui.login-demo",
        package_id="webui.login-demo",
        version="1.0.1",
    )
    state = SimpleNamespace(
        active=[
            SimpleNamespace(
                package_id="webui.login-demo",
                version="1.0.1",
                root=installed_root,
            ),
        ],
        installed=[installed_root],
    )

    detail = hub_detail(
        HubAssetDetail(
            kind="system_plugin",
            asset_id="asset-1",
            version="1.0.1",
            display_name="Auth",
            short_description="Sign in",
            detail_description="# Auth",
            icon_uri="https://example.com/icon.png",
            tags=("login",),
            package_name="webui.login-demo",
        ),
        state,
    )

    assert detail["installedVersion"] == "1.0.1"
    assert detail["active"] is True
    assert detail["enabled"] is True


class _FilePort:
    def __init__(self, content: str | None) -> None:
        self.content = content
        self.calls: list[tuple[str, str, str | None]] = []

    async def list_version_files(
        self, asset_id: str, version: str, *, with_content: str | None = None
    ) -> dict:
        self.calls.append((asset_id, version, with_content))
        files = [
            {"path": "README.md", "size": 6},
            {"path": "assets/avatar.png", "size": 9},
        ]
        return {"files": files, "content": self.content if with_content else None}


def _hub_detail() -> HubAssetDetail:
    return HubAssetDetail(
        kind="system_plugin",
        asset_id="8c64541f99f540949a70d9d69ec08c92",
        version="1.0.0",
        display_name="Auth",
        short_description="Sign in",
        detail_description="",
        icon_uri="",
        tags=(),
        package_name="webui.login-demo",
    )


@pytest.mark.asyncio
async def test_hub_file_tree_and_content_use_the_skillhub_files_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _FilePort("# demo")
    monkeypatch.setattr(hub_catalog, "query_system_plugin", _query(_hub_detail()))
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.marketplace.hub_extensions.create_default_hub_asset_port",
        lambda: port,
    )

    tree = await hub_file_tree("8c64541f99f540949a70d9d69ec08c92")
    content = await hub_file_content("8c64541f99f540949a70d9d69ec08c92", "README.md")

    assert tree == [
        {
            "path": "assets/",
            "type": "dir",
            "children": [
                {"path": "assets/avatar.png", "type": "file", "size": 9, "previewable": False},
            ],
        },
        {"path": "README.md", "type": "file", "size": 6, "previewable": True},
    ]
    assert content == {"path": "README.md", "content": "# demo"}
    assert port.calls[-1] == (
        "8c64541f99f540949a70d9d69ec08c92",
        "1.0.0",
        "README.md",
    )


@pytest.mark.asyncio
async def test_hub_file_content_rejects_paths_escaping_the_package() -> None:
    with pytest.raises(ValueError):
        await hub_file_content("8c64541f99f540949a70d9d69ec08c92", "../secret.md")


@pytest.mark.asyncio
async def test_hub_client_lists_version_files() -> None:
    class Transport:
        def __init__(self) -> None:
            self.requests: list[dict | None] = []

        async def get_data(self, path, *, params=None):
            self.requests.append(params)
            files = [{"path": "README.md", "size": 6}]
            return {"files": files, "content": "# demo" if params else None}

    transport = Transport()
    client = HubClient(transport=transport)

    listing = await client.list_version_files("asset-1", "1.0.0")
    payload = await client.list_version_files("asset-1", "1.0.0", with_content="README.md")

    assert listing["files"] == [{"path": "README.md", "size": 6}]
    assert payload["content"] == "# demo"
    assert transport.requests == [None, {"with_content": "README.md"}]


def _query(detail: HubAssetDetail):
    async def query(asset_id: str, version: str = "") -> HubAssetDetail:
        assert asset_id == detail.asset_id
        assert version in ("", detail.version)
        return detail

    return query


@pytest.mark.parametrize("runtime_active", [False, True])
@pytest.mark.parametrize("origin", ["hub", "user"])
@pytest.mark.parametrize("target_enabled", [False, True])
def test_catalog_uses_restored_manifest_instead_of_install_record(
    tmp_path: Path, origin: str, target_enabled: bool, runtime_active: bool,
) -> None:
    import json

    from jiuwenswarm.common.system_plugin_recovery import SystemPluginRecovery
    from jiuwenswarm.extensions_v2.management.catalog import list_mine

    package_id = "webui.restore-probe"
    install_dir = tmp_path / "installed"
    source = write_package(tmp_path / "source", package_id=package_id, version="1.0.0")
    root = install_dir / package_id / "1.0.0"
    copytree(source, root)
    manifest_path = root / "manifest.json"
    document = json.loads(manifest_path.read_text())
    document["enable"] = target_enabled
    manifest_path.write_text(json.dumps(document))
    recovery = SystemPluginRecovery(install_dir, tmp_path / "recovery")
    point = recovery.begin_session("catalog-test")
    document["enable"] = not target_enabled
    manifest_path.write_text(json.dumps(document))
    store = ExtensionInstallState.for_install_dir(install_dir)
    store.upsert(ExtensionInstallRecord(
        package_id=package_id, version="1.0.0", origin=origin,
        enabled=not target_enabled, active=not target_enabled,
    ))

    recovery.restore(point["id"])
    active = [SimpleNamespace(package_id=package_id, version="1.0.0", root=root)] if runtime_active else []
    state = SimpleNamespace(active=active, installed=[root])
    card = list_mine(tmp_path / "examples", state, install_dir=install_dir)[0]
    entry = entry_from_root(root)
    assert entry is not None
    detail = project_detail(entry, state, install_dir=install_dir)

    assert store.get(package_id).enabled is not target_enabled
    for result in (card, detail):
        assert result["enabled"] is target_enabled
        assert result["active"] is runtime_active
        assert result["origin"] == origin


@pytest.mark.parametrize("domain", ["agentserver", "gateway"])
@pytest.mark.parametrize("runtime_active", [False, True])
@pytest.mark.parametrize("has_origin", [False, True])
@pytest.mark.parametrize("target_enabled", [False, True])
def test_hub_views_follow_restored_manifest(
    tmp_path: Path, domain: str, runtime_active: bool, has_origin: bool, target_enabled: bool,
) -> None:
    import json

    from jiuwenswarm.common.system_plugin_recovery import SystemPluginRecovery
    from jiuwenswarm.gateway.extensions_v2.management import catalog as gateway_catalog

    package_id = "webui.login-demo"
    install_dir = tmp_path / "installed"
    source = write_package(tmp_path / "source", package_id=package_id, version="1.0.0")
    root = install_dir / package_id / "1.0.0"
    copytree(source, root)
    manifest = root / "manifest.json"
    document = json.loads(manifest.read_text())
    document["package"]["domain"] = domain
    document["enable"] = target_enabled
    manifest.write_text(json.dumps(document))
    recovery = SystemPluginRecovery(install_dir, tmp_path / "recovery")
    point = recovery.begin_session("hub-restore-test")
    document["enable"] = not target_enabled
    manifest.write_text(json.dumps(document))
    store = ExtensionInstallState.for_install_dir(install_dir)
    if has_origin:
        store.upsert(ExtensionInstallRecord(
            package_id=package_id, version="1.0.0", origin="hub", asset_id="asset-1",
            enabled=not target_enabled, active=not target_enabled,
        ))
    recovery.restore(point["id"])
    active = [SimpleNamespace(package_id=package_id, version="1.0.0", root=root)] if runtime_active else []
    state = SimpleNamespace(active=active, installed=[root])
    item = HubAssetSummary(
        kind="system_plugin", asset_id="asset-1", display_name="Auth", short_description="",
        public_latest_version="1.0.0", icon_uri="", tags=(), package_name=package_id,
    )
    detail = _hub_detail()
    results = [hub_card(item, build_state_index(state), store), hub_detail(detail, state, store)]
    if domain == "gateway":
        entry = gateway_catalog.MarketEntry(package_id, "1.0.0", root, document)
        facts = gateway_catalog._Facts(
            active={record.package_id: {"version": record.version, "root": root} for record in active},
            installed={package_id: root},
            roots={package_id: (root,)},
        )
        results.append(gateway_catalog._summary(entry, facts, origin="hub", origin_record=store.get(package_id)))
    for result in results:
        assert result["enabled"] is target_enabled
        assert result["active"] is runtime_active


@pytest.mark.asyncio
@pytest.mark.parametrize("has_origin", [False, True])
async def test_hub_list_reads_disabled_gateway_missing_from_agent_index(tmp_path, monkeypatch, has_origin):
    import json

    package_id = "webui.login-demo"
    install_dir = tmp_path / "installed"
    source = write_package(tmp_path / "source", package_id=package_id, version="1.0.0")
    root = install_dir / package_id / "1.0.0"
    copytree(source, root)
    document = json.loads((root / "manifest.json").read_text())
    document["package"]["domain"] = "gateway"
    document["enable"] = False
    (root / "manifest.json").write_text(json.dumps(document))
    store = ExtensionInstallState.for_install_dir(install_dir)
    if has_origin:
        store.upsert(ExtensionInstallRecord(
            package_id=package_id, version="1.0.0", origin="hub", asset_id="asset-1",
            install_path=str(root), enabled=True, active=True,
        ))
    item = HubAssetSummary(
        kind="system_plugin", asset_id="asset-1", display_name="Auth", short_description="",
        public_latest_version="1.0.0", icon_uri="", tags=(), package_name=package_id,
    )
    async def cached(*args, **kwargs):
        return [item], None
    monkeypatch.setattr("jiuwenswarm.server.runtime.marketplace.hub_catalog_cache.cached_asset_catalog", cached)
    cards, _ = await hub_catalog.list_hub_system_plugins(SimpleNamespace(active=[], installed=[]), install_dir)
    assert cards[0]["enabled"] is False

    detail = hub_detail(_hub_detail(), SimpleNamespace(active=[], installed=[]), store, install_dir=install_dir)
    assert detail["enabled"] is False
    assert detail["installed"] is True
