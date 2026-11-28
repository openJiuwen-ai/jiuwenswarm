"""Hub app install, consent, update and uninstall regressions."""

from __future__ import annotations

import asyncio
import base64
import errno
import hashlib
import io
import os
import shutil
import stat
import subprocess
import zipfile
from dataclasses import asdict
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.extensions.app_market import hub_install
from jiuwenswarm.extensions.app_market.app_market.models import AppInstance, AppManifest, platform_key
from jiuwenswarm.extensions.app_market.app_market.market_service import MarketService, _application_env
from jiuwenswarm.extensions.app_market.manager import (
    AppMarketError,
    AppMarketManager,
    RunConsentRequired,
)
from jiuwenswarm.server.runtime.marketplace.hub_asset_port import (
    HubAssetDetail,
    HubAssetQuery,
    HubResolvedDownload,
    HubSearchPage,
)
from jiuwenswarm.server.runtime.marketplace.hub_package_downloader import (
    HubPackageDownloader,
)

OWNER = "alice"
ASSET = "0f1e2d3c4b5a"
HUB_URL = "https://hub.example.test"
DOWNLOAD_URL = "https://downloads.example.test/gomoku.zip"
ICON_URL = "https://downloads.example.test/icon.png?X-Amz-Expires=60"
PNG_ICON = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRuoAAAAASUVORK5CYII="
)
SVG_ICON = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><circle cx="5" cy="5" r="5"/></svg>'


def _app_zip(version: str = "1.2.3", marker: str = "v1", *, wrapper_icon: bool = False) -> bytes:
    stream = io.BytesIO()
    html = (
        "<!doctype html><html><head>"
        '<meta name="application-name" content="五子棋">'
        '<meta name="description" content="对弈应用">'
        f'<meta name="application-version" content="{version}">'
        '<link rel="icon" href="assets/icon.svg">'
        "</head><body><script>/* WorkSwarmApp */</script></body></html>"
    )
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("gomoku/plugin.yaml", f"name: gomoku\nversion: {version}\n")
        if wrapper_icon:
            archive.writestr("gomoku/icon.png", base64.b64decode(PNG_ICON))
        archive.writestr("gomoku/gomoku/index.html", html)
        archive.writestr("gomoku/gomoku/assets/icon.svg", SVG_ICON)
        archive.writestr("gomoku/gomoku/assets/app.js", f"const marker = '{marker}';\n")
        archive.writestr("gomoku/gomoku/scripts/app_server.py", "print('ok')\n")
        archive.writestr("gomoku/gomoku/README.md", f"# gomoku {version}\n")
    return stream.getvalue()


class FakePort:
    base_url = HUB_URL

    def __init__(self, payload: bytes, version: str = "1.2.3", *, requires_project: bool = False):
        self.payload = payload
        self.version = version
        self.requires_project = requires_project
        self.icon_uri = ""
        self.checksum = hashlib.sha256(payload).hexdigest()

    async def search_assets(self, request) -> HubSearchPage:
        from jiuwenswarm.server.runtime.marketplace.hub_asset_port import HubAssetSummary

        detail = await self.query_asset(HubAssetQuery(kind="app", asset_id=ASSET))
        return HubSearchPage(
            items=(
                HubAssetSummary(
                    kind="app",
                    asset_id=detail.asset_id,
                    display_name=detail.display_name,
                    short_description=detail.short_description,
                    public_latest_version=detail.version,
                    icon_uri=detail.icon_uri,
                    tags=detail.tags,
                    package_name=detail.package_name,
                ),
            ),
            total=1,
            page=1,
            page_size=20,
        )

    async def query_asset(self, request: HubAssetQuery) -> HubAssetDetail:
        assert request.kind == "app"
        assert request.asset_id == ASSET
        version = request.version or self.version
        return HubAssetDetail(
            kind="app",
            asset_id=ASSET,
            version=version,
            display_name="五子棋 · Gomoku",
            short_description="对弈应用",
            detail_description="",
            icon_uri=self.icon_uri,
            tags=("game",),
            package_name="gomoku",
            file_size=len(self.payload),
            source_size=len(self.payload),
            requires_project=self.requires_project,
            permissions=("ai",),
            has_server_script=True,
            checksum_sha256=self.checksum,
        )

    async def resolve_download(self, request) -> HubResolvedDownload:
        return HubResolvedDownload(
            kind="app",
            asset_id=ASSET,
            version=request.version,
            download_url=DOWNLOAD_URL,
            checksum_sha256=self.checksum,
            package_name="gomoku",
            file_size=len(self.payload),
        )


def _downloader(payload: bytes) -> HubPackageDownloader:
    async def fetch(_url: str) -> bytes:
        return payload

    return HubPackageDownloader(
        fetch_bytes=fetch,
        allowed_download_hosts=("downloads.example.test",),
        max_download_bytes=hub_install.MAX_APP_ZIP_BYTES,
        max_total_uncompressed_bytes=hub_install.MAX_APP_SOURCE_BYTES,
        max_single_file_bytes=hub_install.MAX_APP_SINGLE_FILE_BYTES,
        verify_archive_crc=False,
    )


def _manager(tmp_path: Path) -> AppMarketManager:
    return AppMarketManager(str(tmp_path / "market"))


def test_install_writes_manifest_scripts_and_install_record(tmp_path):
    payload = _app_zip(wrapper_icon=True)
    manager = _manager(tmp_path)
    port = FakePort(payload)
    port.icon_uri = ICON_URL
    result = asyncio.run(
        hub_install.install_hub_app(
            manager=manager,
            asset_id=ASSET,
            install_root=tmp_path / "apps",
            owner_id=OWNER,
            hub_port=port,
            downloader=_downloader(payload),
        )
    )

    app_id = result["app_id"]
    assert app_id == hub_install.app_install_id(ASSET, OWNER, HUB_URL)
    destination = Path(result["work_dir"])
    assert destination == (tmp_path / "apps" / "gomoku").resolve()
    assert (destination / "index.html").is_file()
    # 启停脚本按当前平台生成（windows 为 .ps1，其余为 .sh）。
    expected_start = "start.ps1" if platform_key() == "windows" else "start.sh"
    assert (destination / "scripts" / expected_start).is_file()
    manifest = manager.service.get_app(app_id)
    assert manifest is not None
    assert manifest.source == "hub"
    assert manifest.hub_asset_id == ASSET
    assert manifest.artifact_sha256 == port.checksum
    assert manifest.permissions == ["ai"]
    assert manifest.icon_url == f"data:image/png;base64,{PNG_ICON}"
    assert manager.list_apps(OWNER)["apps"][0]["icon_url"] == manifest.icon_url
    assert manager.service._skill_adapter.has_generated_static_start(manifest)
    record = manager.storage.load_install_record(app_id)
    assert record is not None
    assert record["package_name"] == "gomoku"
    assert record["version"] == "1.2.3"
    assert record["artifact_sha256"] == port.checksum
    assert record["work_dir"] == str(destination)


def test_list_legacy_hub_app_uses_installed_icon(tmp_path):
    manager = _manager(tmp_path)
    root = tmp_path / "installed"
    root.mkdir()
    (root / "index.html").write_text('<link rel="icon" href="icon.svg">', encoding="utf-8")
    (root / "icon.svg").write_text(SVG_ICON, encoding="utf-8")
    manifest = AppManifest(
        app_id="hub-legacy",
        name="Legacy app",
        version="1.2.3",
        run_type="static",
        work_dir=str(root),
        description="Description",
        owner_id=OWNER,
        source="hub",
        icon_url=ICON_URL,
    )
    manager.storage.save_manifest(manifest.app_id, manifest)

    icon = manager.list_apps(OWNER)["apps"][0]["icon_url"]
    assert icon == "data:image/svg+xml;base64," + base64.b64encode(SVG_ICON.encode()).decode()
    assert manager.storage.load_manifest(manifest.app_id).icon_url == ICON_URL

    (root / "index.html").write_text('<link rel="icon" href="../icon.svg">', encoding="utf-8")
    (tmp_path / "icon.svg").write_text(SVG_ICON, encoding="utf-8")
    assert manager.list_apps(OWNER)["apps"][0]["icon_url"] == ""


def test_install_rejects_existing_destination(tmp_path):
    payload = _app_zip()
    manager = _manager(tmp_path)
    port = FakePort(payload)
    asyncio.run(
        hub_install.install_hub_app(
            manager=manager,
            asset_id=ASSET,
            install_root=tmp_path / "apps",
            owner_id=OWNER,
            hub_port=port,
            downloader=_downloader(payload),
        )
    )
    with pytest.raises(hub_install.AppInstallError) as excinfo:
        asyncio.run(
            hub_install.install_hub_app(
                manager=manager,
                asset_id=ASSET,
                install_root=tmp_path / "apps",
                owner_id=OWNER,
                hub_port=port,
                downloader=_downloader(payload),
            )
        )
    assert excinfo.value.code == "install_path_conflict"


def test_package_root_requires_market_wrapper(tmp_path):
    extracted = tmp_path / "extract"
    (extracted / "gomoku").mkdir(parents=True)
    (extracted / "gomoku" / "index.html").write_text("<html></html>", encoding="utf-8")
    with pytest.raises(hub_install.AppInstallError) as excinfo:
        hub_install.app_package_root(extracted, "gomoku")
    assert excinfo.value.code == "invalid_plugin_structure"


def test_run_consent_is_bound_to_artifact_digest(tmp_path):
    payload = _app_zip()
    manager = _manager(tmp_path)
    port = FakePort(payload)
    result = asyncio.run(
        hub_install.install_hub_app(
            manager=manager,
            asset_id=ASSET,
            install_root=tmp_path / "apps",
            owner_id=OWNER,
            hub_port=port,
            downloader=_downloader(payload),
        )
    )
    app_id = result["app_id"]
    manifest = manager.service.get_app(app_id)

    with pytest.raises(RunConsentRequired) as excinfo:
        manager._require_run_consent(app_id, OWNER, None)
    assert excinfo.value.artifact_sha256 == manifest.artifact_sha256

    with pytest.raises(AppMarketError) as mismatch:
        manager._require_run_consent(
            app_id, OWNER, {"artifact_sha256": "0" * 64}
        )
    assert mismatch.value.code == "app_artifact_changed"

    manager._require_run_consent(
        app_id, OWNER, {"artifact_sha256": manifest.artifact_sha256}
    )
    manager._require_run_consent(app_id, OWNER, None)
    assert manager._consents.has(OWNER, app_id, manifest.artifact_sha256)
    assert not manager._consents.has("bob", app_id, manifest.artifact_sha256)


def test_requires_project_injection_and_start_guard(tmp_path):
    manifest = AppManifest(
        app_id="app",
        name="app",
        version="1.0.0",
        run_type="static",
        work_dir=str(tmp_path),
        requires_project=True,
        project_id="proj_1",
        project_dir=str(tmp_path),
        project_work_mode="work",
    )
    env = MarketService._project_env(manifest)
    assert env["WORKSWARM_APP_PROJECT_ID"] == "proj_1"
    assert env["WORKSWARM_APP_WORK_MODE"] == "work"
    assert env["WORKSWARM_APP_PROJECT_DIR"] == str(tmp_path)

    empty = MarketService._project_env(None)
    assert empty["WORKSWARM_APP_PROJECT_ID"] == ""


def test_convert_hub_app_keeps_one_registration_and_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_MARKET_INSTALL_ROOT", str(tmp_path / "default-apps"))
    manager = _manager(tmp_path)
    payload = _app_zip()
    installed = asyncio.run(hub_install.install_hub_app(
        manager=manager, asset_id=ASSET, install_root=tmp_path / "installs",
        owner_id=OWNER, hub_port=FakePort(payload, requires_project=True), downloader=_downloader(payload),
    ))
    app_id = installed["app_id"]
    source_dir = Path(installed["work_dir"])
    entry = source_dir / "index.html"
    (source_dir / "data").mkdir()
    (source_dir / "data" / "save.json").write_text("{}", encoding="utf-8")
    (source_dir / ".git").mkdir()
    manifest = manager.service.get_app(app_id)
    manifest.project_id = "existing-project"
    manifest.project_dir = str(source_dir)
    manifest.project_work_mode = "code"
    manifest.source_session = "source-session"
    manifest.editor_session_id = "edit-current"
    manifest.editor_session_ids = ["edit-current"]
    manager.storage.save_manifest(app_id, manifest)

    with pytest.raises(ValueError, match="Convert this Hub app"):
        asyncio.run(manager.publish_app(str(entry), "session", OWNER))
    with pytest.raises(AppMarketError, match="Installed application not found"):
        asyncio.run(manager.convert_to_local(app_id, "bob"))
    manager.service.allow_hub_code = False
    with pytest.raises(AppMarketError, match="disabled for this deployment"):
        asyncio.run(manager.convert_to_local(app_id, OWNER))
    manager.service.allow_hub_code = True

    converted = asyncio.run(manager.convert_to_local(app_id, OWNER))
    assert converted["app_id"] == app_id
    assert len(manager.storage.list_apps()) == 1
    local = manager.service.get_app(app_id)
    assert local.source == "local" and local.work_dir == str(source_dir)
    assert local.hub_asset_id == local.hub_url == local.artifact_sha256 == ""
    assert local.project_id == "existing-project" and local.requires_project and local.permissions == ["ai"]
    assert local.source_session == "source-session"
    assert manager.get_editor_app("edit-current", OWNER)["app_id"] == app_id
    assert (source_dir / "data" / "save.json").is_file() and (source_dir / ".git").is_dir()
    assert manager.storage.load_install_record(app_id) is None
    assert manager._installed_hub_app(ASSET, OWNER, HUB_URL)[1] is None
    with pytest.raises(AppMarketError) as update_error:
        asyncio.run(manager.start_update(app_id=app_id, owner_id=OWNER))
    assert update_error.value.code == "app_not_hub_installed"
    assert "Only Hub-installed applications" in str(update_error.value)
    with pytest.raises(hub_install.AppInstallError) as uninstall_error:
        asyncio.run(hub_install.uninstall_hub_app(
            manager=manager, app_id=app_id, owner_id=OWNER, remove_files=True,
        ))
    assert uninstall_error.value.code == "app_not_hub_installed"
    assert "delete the local app instead" in str(uninstall_error.value)

    republished = asyncio.run(manager.publish_app(str(entry), "session", OWNER))
    assert republished["app_id"] == app_id
    assert len(manager.storage.list_apps()) == 1
    assert (source_dir / "data" / "save.json").is_file()
    removed = asyncio.run(
        manager.delete_app(app_id, OWNER, remove_files=True, confirm_work_dir=str(source_dir))
    )
    assert removed["removed_files"] is True
    assert not source_dir.exists()
    archive = Path(removed["preserved_dir"])
    assert (archive / "data" / "save.json").is_file()
    assert (archive / ".git").is_dir()


def test_hub_registration_does_not_inherit_project_or_editor(tmp_path):
    manager = _manager(tmp_path)
    app_dir = tmp_path / "installs" / "copy"
    app_dir.mkdir(parents=True)
    (app_dir / "index.html").write_text("<html></html>", encoding="utf-8")
    app_id = "hub-inherited"
    manager.storage.save_manifest(app_id, AppManifest(
        app_id=app_id,
        name="Old Copy",
        version="1.0.0",
        run_type="static",
        work_dir=str(app_dir),
        owner_id=OWNER,
        source="local",
        project_id="proj_old",
        project_dir=str(tmp_path / "old-temp"),
        project_work_mode="code",
        editor_session_id="edit-old",
        editor_create_token="token-old",
        editor_session_ids=["edit-old"],
    ))

    published = asyncio.run(manager.service.publish_app(str(app_dir), {
        "app_id": app_id,
        "name": "Hub Copy",
        "version": "1.0.0",
        "run_type": "static",
        "work_dir": str(app_dir),
        "owner_id": OWNER,
        "source": "hub",
        "hub_asset_id": ASSET,
        "hub_url": HUB_URL,
        "artifact_sha256": "a" * 64,
        "requires_project": True,
    }))
    assert published is not None
    manifest = manager.storage.load_manifest(app_id)
    assert manifest.source == "hub"
    assert manifest.project_id == ""
    assert manifest.project_dir == ""
    assert manifest.project_work_mode == ""
    assert not manifest.source_session
    assert manifest.editor_session_id == ""
    assert manifest.editor_create_token == ""
    assert manifest.editor_session_ids == []


def test_convert_detaches_stale_project_and_binds_app_directory(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    manager.service.allow_hub_code = True
    payload = _app_zip()
    installed = asyncio.run(hub_install.install_hub_app(
        manager=manager, asset_id=ASSET, install_root=tmp_path / "installs",
        owner_id=OWNER, hub_port=FakePort(payload, requires_project=True),
        downloader=_downloader(payload),
    ))
    app_id = installed["app_id"]
    source_dir = Path(installed["work_dir"])
    manifest = manager.service.get_app(app_id)
    manifest.project_id = "proj_old"
    manifest.project_dir = str(tmp_path / "old-temp")
    manifest.project_work_mode = "code"
    manifest.source_session = "old-session"
    manifest.editor_session_id = "edit-old"
    manifest.editor_create_token = "token-old"
    manifest.editor_session_ids = ["edit-old"]
    manager.storage.save_manifest(app_id, manifest)
    manager.storage.save_instance(AppInstance("running-old", app_id, state="running", owner_id=OWNER))

    async def stop_old_instance(instance_id):
        assert instance_id == "running-old"
        assert manager.service.get_app(app_id).project_id == "proj_old"
        instance = manager.service.get_instance(instance_id)
        instance.state = "stopped"
        manager.storage.save_instance(instance)
        return True

    monkeypatch.setattr(manager.service, "stop_app", stop_old_instance)

    calls = []

    async def request(method, params):
        calls.append((method, params))
        if method == ReqMethod.PROJECT_LIST:
            return True, {
                "projects": [{
                    "project_id": "proj_old",
                    "project_dir": str(tmp_path / "old-temp"),
                    "work_mode": "code",
                }]
            }
        if method == ReqMethod.PROJECT_CREATE:
            assert Path(params["project_dir"]) == source_dir
            return True, {
                "project": {
                    "project_id": "proj_new",
                    "project_dir": params["project_dir"],
                    "work_mode": params["work_mode"],
                }
            }
        raise AssertionError(method)

    converted = asyncio.run(
        manager.convert_to_local(app_id, OWNER, project_request=request)
    )
    saved = manager.service.get_app(app_id)
    assert converted["project_id"] == "proj_new"
    assert saved.project_id == "proj_new"
    assert Path(saved.project_dir) == source_dir
    assert saved.source_session == ""
    assert saved.editor_session_id == ""
    assert manager.service.get_instance("running-old").state == "stopped"
    assert any(method == ReqMethod.PROJECT_CREATE for method, _ in calls)


def test_delete_waits_for_conversion_and_registration_stays_removed(tmp_path):
    manager = _manager(tmp_path)
    app_id = "hub-converting"
    source = tmp_path / "app"
    source.mkdir()
    manager.storage.save_manifest(app_id, AppManifest(
        app_id=app_id,
        name="App",
        version="1.0.0",
        run_type="static",
        work_dir=str(source),
        owner_id=OWNER,
        source="hub",
    ))

    async def convert_and_delete():
        binding_started = asyncio.Event()
        release_binding = asyncio.Event()

        async def request(method, params):
            assert method == ReqMethod.PROJECT_LIST
            binding_started.set()
            await release_binding.wait()
            return True, {"projects": [{"project_id": "project", "project_dir": str(source), "work_mode": "code"}]}

        conversion = asyncio.create_task(manager.convert_to_local(app_id, OWNER, project_request=request))
        await asyncio.wait_for(binding_started.wait(), timeout=5)
        deletion = asyncio.create_task(manager.delete_app(app_id, OWNER))
        await asyncio.sleep(0)
        deleted_during_binding = deletion.done()
        release_binding.set()
        converted, deleted = await asyncio.gather(conversion, deletion)
        assert not deleted_during_binding
        assert converted["project_id"] == "project"
        assert deleted["deleted"] is True

    asyncio.run(convert_and_delete())
    assert manager.service.get_app(app_id) is None
    assert source.is_dir()


def test_update_preserves_data_git_and_project_binding(tmp_path):
    payload_v1 = _app_zip("1.0.0", "v1", wrapper_icon=True)
    manager = _manager(tmp_path)
    port = FakePort(payload_v1, version="1.0.0")
    result = asyncio.run(
        hub_install.install_hub_app(
            manager=manager,
            asset_id=ASSET,
            install_root=tmp_path / "apps",
            owner_id=OWNER,
            hub_port=port,
            downloader=_downloader(payload_v1),
        )
    )
    app_id = result["app_id"]
    destination = Path(result["work_dir"])
    (destination / "data").mkdir()
    (destination / "data" / "save.json").write_text("{}", encoding="utf-8")
    (destination / ".git").mkdir()
    (destination / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    manifest = manager.service.get_app(app_id)
    (destination / "assets" / "app.js").write_text("local edit", encoding="utf-8")
    (destination / "new-resource.txt").write_text("untracked", encoding="utf-8")
    manifest.project_id = "proj_1"
    manifest.project_dir = str(destination)
    manifest.project_work_mode = "code"
    manifest.pinned = True
    manifest.created_at = "2026-01-01T00:00:00+00:00"
    manifest.editor_session_id = "edit-old"
    manifest.editor_create_token = "token-old"
    manifest.editor_session_ids = ["edit-old"]
    manager.service.storage.save_manifest(app_id, manifest)
    assert manager.set_app_pinned(app_id, OWNER, True) == {"app_id": app_id, "pinned": True}
    created_before = manager.service.get_app(app_id).created_at

    payload_v2 = _app_zip("2.0.0", "v2")
    port2 = FakePort(payload_v2, version="2.0.0")
    port2.icon_uri = ICON_URL
    port2.base_url = "https://other.example.test"
    with pytest.raises(hub_install.AppInstallError, match="original Hub"):
        asyncio.run(hub_install.update_hub_app(
            manager=manager, app_id=app_id, owner_id=OWNER, hub_port=port2,
            downloader=_downloader(payload_v2),
        ))
    assert manager.storage.load_install_record(app_id)["artifact_sha256"] == port.checksum
    port2.base_url = HUB_URL
    updated = asyncio.run(
        hub_install.update_hub_app(
            manager=manager,
            app_id=app_id,
            owner_id=OWNER,
            hub_port=port2,
            downloader=_downloader(payload_v2),
        )
    )
    assert updated["updated"] is True
    backup = Path(updated["backup_dir"])
    assert (backup / "assets" / "app.js").read_text(encoding="utf-8") == "local edit"
    assert (backup / "new-resource.txt").read_text(encoding="utf-8") == "untracked"
    assert (backup / "data" / "save.json").is_file()
    assert (destination / "data" / "save.json").read_text(encoding="utf-8") == "{}"
    assert (destination / ".git" / "HEAD").is_file()
    manifest = manager.service.get_app(app_id)
    assert manifest.version == "2.0.0"
    assert manifest.icon_url == "data:image/svg+xml;base64," + base64.b64encode(SVG_ICON.encode()).decode()
    assert manifest.project_id == "proj_1"
    assert manifest.project_dir == str(destination)
    assert manifest.project_work_mode == "code"
    assert manifest.pinned is True
    assert manifest.created_at == "2026-01-01T00:00:00+00:00"
    assert manifest.editor_session_id == "edit-old"
    assert manifest.editor_create_token == "token-old"
    assert manifest.editor_session_ids == ["edit-old"]
    assert manifest.artifact_sha256 == port2.checksum
    assert manifest.pinned is True, "updating a Hub app must keep the user pin"
    assert manifest.created_at == created_before, "updates must not reset the registration time"
    assert "v2" in (destination / "assets" / "app.js").read_text(encoding="utf-8")
    assert manager.storage.load_install_record(app_id)["version"] == "2.0.0"


@pytest.mark.parametrize(("history", "busy_on_check"), [(True, 1), (True, 2), (True, 3), (False, 1)])
def test_update_refuses_any_busy_editor_before_directory_swap(tmp_path, history, busy_on_check):
    manager = _manager(tmp_path)
    original = _app_zip("1.0.0", "v1")
    installed = asyncio.run(hub_install.install_hub_app(
        manager=manager, asset_id=ASSET, install_root=tmp_path / "apps", owner_id=OWNER,
        hub_port=FakePort(original, version="1.0.0"), downloader=_downloader(original),
    ))
    app_id = installed["app_id"]
    manifest = manager.storage.load_manifest(app_id)
    manifest.editor_session_id = "latest"
    if history:
        manifest.editor_session_ids = ["older", "latest"]
    manager.storage.save_manifest(app_id, manifest)
    editor_id = "older" if history else "latest"
    checks = 0

    def editor_is_busy(session_id):
        nonlocal checks
        if session_id != editor_id:
            return False
        checks += 1
        return checks == busy_on_check

    replacement = _app_zip("2.0.0", "v2")
    with pytest.raises(hub_install.AppInstallError, match="Stop the active editor session") as failure:
        asyncio.run(hub_install.update_hub_app(
            manager=manager, app_id=app_id, owner_id=OWNER,
            hub_port=FakePort(replacement, version="2.0.0"),
            downloader=_downloader(replacement), editor_is_busy=editor_is_busy,
        ))
    assert failure.value.code == "app_operation_conflict"
    assert checks == busy_on_check
    assert manager.storage.load_manifest(app_id) == manifest
    assert "v1" in (Path(installed["work_dir"]) / "assets" / "app.js").read_text(encoding="utf-8")


@pytest.mark.parametrize("requires_project", [False, True])
def test_install_binds_project_before_conversion(tmp_path, monkeypatch, requires_project):
    manager = _manager(tmp_path)
    manager.service.allow_hub_code = True
    payload = _app_zip()
    port = FakePort(payload, requires_project=requires_project)
    monkeypatch.setattr(manager, "_hub_port", lambda: port)
    monkeypatch.setattr(hub_install, "hub_app_downloader", lambda: _downloader(payload))
    calls = []
    source_dir = tmp_path / "installs" / "gomoku"
    install_params = {"asset_id": ASSET, "install_dir": str(source_dir.parent), "owner_id": OWNER}

    async def request(method, params):
        calls.append((method, params))
        if method == ReqMethod.PROJECT_LIST:
            return True, {"projects": []}
        if method == ReqMethod.PROJECT_CREATE:
            assert Path(params["project_dir"]) == source_dir
            assert source_dir.is_dir()
            existing = await manager.start_install(**install_params)
            assert existing["state"] == "running"
            assert existing["job_id"]
            return True, {
                "project": {
                    "project_id": "proj_app",
                    "project_dir": params["project_dir"],
                    "work_mode": params["work_mode"],
                }
            }
        raise AssertionError(method)

    async def install():
        response = await manager.start_install(**install_params, project_request=request)
        job_id = response["job_id"]
        await manager._get_job(job_id, OWNER).task
        status = manager.install_status(job_id, OWNER)
        assert status["state"] == "done", status
        return status["result"]

    installed = asyncio.run(install())
    app_id = installed["app_id"]
    before = manager.storage.load_manifest(app_id)
    assert before.requires_project is requires_project
    assert installed["project_id"] == before.project_id == "proj_app"
    converted = asyncio.run(manager.convert_to_local(app_id, OWNER))
    saved = manager.service.storage.load_manifest(app_id)
    assert converted["project_id"] == "proj_app"
    assert saved.project_id == "proj_app"
    assert saved.project_dir == str(source_dir)
    assert saved.project_work_mode == "code"
    assert [method for method, _ in calls] == [
        ReqMethod.PROJECT_LIST,
        ReqMethod.PROJECT_CREATE,
    ]


def test_install_keeps_app_when_project_binding_fails(tmp_path):
    manager = _manager(tmp_path)
    manager.service.allow_hub_code = True
    payload = _app_zip()
    port = FakePort(payload, requires_project=True)

    async def failing_request(method, params):
        return False, {"code": "project_rpc_unavailable", "message": "agent unavailable"}

    async def run():
        return await hub_install.install_hub_app(
            manager=manager,
            asset_id=ASSET,
            install_root=tmp_path / "installs",
            owner_id=OWNER,
            hub_port=port,
            downloader=_downloader(payload),
            project_request=failing_request,
        )

    installed = asyncio.run(run())
    app_id = installed["app_id"]
    destination = Path(installed["work_dir"])
    # A failed bind must not discard a package that is already installed.
    assert destination.is_dir()
    assert (destination / "index.html").is_file()
    assert installed["project_id"] == ""
    manifest = manager.storage.load_manifest(app_id)
    assert manifest is not None and not manifest.project_id
    assert manager.storage.load_install_record(app_id) is not None


def test_install_stays_done_when_project_binding_is_cancelled(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    payload = _app_zip()
    port = FakePort(payload, requires_project=True)
    monkeypatch.setattr(manager, "_hub_port", lambda: port)
    monkeypatch.setattr(hub_install, "hub_app_downloader", lambda: _downloader(payload))
    install_params = {"asset_id": ASSET, "install_dir": str(tmp_path / "installs"), "owner_id": OWNER}

    async def run():
        binding_started = asyncio.Event()

        async def request(method, params):
            assert method == ReqMethod.PROJECT_LIST
            binding_started.set()
            await asyncio.Future()

        response = await manager.start_install(**install_params, project_request=request)
        job_id, app_id = response["job_id"], response["app_id"]
        task = manager._get_job(job_id, OWNER).task
        await asyncio.wait_for(binding_started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        status = manager.install_status(job_id, OWNER)
        assert status["state"] == status["phase"] == "done"
        assert not status["cancellable"]
        assert status["result"]["app_id"] == app_id
        assert status["result"]["project_id"] == ""
        assert (Path(status["result"]["work_dir"]) / "index.html").is_file()
        assert manager.storage.load_install_record(app_id) is not None
        assert manager.storage.load_manifest(app_id).project_id == ""
        retry = await manager.start_install(**install_params)
        assert retry["app_id"] == app_id and retry["already_installed"]

    asyncio.run(run())


@pytest.mark.parametrize("launch_fails", [False, True])
def test_local_app_with_recorded_owner_preserves_ownership(tmp_path, monkeypatch, launch_fails):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "personal")

    manager = _manager(tmp_path)
    page = tmp_path / "gomoku.html"
    page.write_text('<html><meta name="description" content="Play"><body>gomoku</body></html>')

    async def run():
        app = await manager.publish_app(str(page), "session", owner_id=OWNER)
        app_id = app["app_id"]
        manifest = manager.storage.load_manifest(app_id)
        manifest.project_id = "proj_local"
        manifest.project_dir = str(tmp_path)
        manifest.project_work_mode = "code"
        manager.storage.save_manifest(app_id, manifest)
        spawn = AsyncMock(return_value=1234)
        if launch_fails:
            spawn.side_effect = OSError("spawn failed")
        monkeypatch.setattr(manager._service._supervisor, "start", spawn)
        monkeypatch.setattr(manager._service, "_wait_for_running", AsyncMock(return_value=True))

        started = await manager.start_app(app_id, owner_id="")

        if launch_fails:
            assert started is None
        else:
            assert started and started["state"] == "running"
        assert spawn.await_count == 1
        assert manager.storage.load_manifest(app_id).owner_id == OWNER
        assert [app["app_id"] for app in manager.list_apps(OWNER)["apps"]] == [app_id]

        monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
        with pytest.raises(AppMarketError) as failure:
            await manager.start_app(app_id, owner_id="")
        assert failure.value.code == "permission_denied"

    asyncio.run(run())


def test_legacy_hub_install_binds_project_before_launch(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    manager.service.allow_hub_code = True
    payload = _app_zip()
    port = FakePort(payload, requires_project=False)

    async def run():
        installed = await hub_install.install_hub_app(
            manager=manager, asset_id=ASSET, install_root=tmp_path / "installs",
            owner_id=OWNER, hub_port=port, downloader=_downloader(payload),
        )
        app_id = installed["app_id"]
        manifest = manager.storage.load_manifest(app_id)
        assert not manifest.requires_project and not manifest.project_id
        spawn = AsyncMock(return_value=1234)
        monkeypatch.setattr(manager.service._supervisor, "start", spawn)
        monkeypatch.setattr(manager.service, "_wait_for_running", AsyncMock(return_value=True))

        async def request(method, params):
            if method == ReqMethod.PROJECT_LIST:
                return True, {"projects": []}
            assert method == ReqMethod.PROJECT_CREATE
            return True, {"project": {
                "project_id": "proj_app", "project_dir": params["project_dir"], "work_mode": "code",
            }}

        started = await manager.start_app(app_id, OWNER, {"artifact_sha256": port.checksum}, request)
        assert started["state"] == "running"
        env = spawn.await_args.kwargs["env"]
        assert env["WORKSWARM_APP_PROJECT_ID"] == "proj_app"
        assert env["WORKSWARM_APP_WORK_MODE"] == "code"
        assert manager.storage.load_manifest(app_id).project_id == "proj_app"

    asyncio.run(run())


def test_recovery_does_not_delete_an_unowned_target_or_backup(tmp_path):
    manager = _manager(tmp_path)
    root = (tmp_path / "apps").resolve()
    root.mkdir()
    destination = root / "gomoku"
    ops = tmp_path / "market" / "install_ops"
    operation = hub_install._prepare_operation(ops, "job", "hub-review", root, destination, "install")
    destination.mkdir()
    (destination / "keep.txt").write_text("user files", encoding="utf-8")
    old_backup = root / "photos.backup-2025"
    old_backup.mkdir()

    assert len(hub_install.recover_interrupted_operations(ops, manager.storage)) == 1
    assert (destination / "keep.txt").read_text(encoding="utf-8") == "user files"
    assert old_backup.is_dir()
    assert not Path(operation["stage"]).exists()


def test_recovery_rolls_back_a_partial_owned_install(tmp_path):
    manager = _manager(tmp_path)
    root = (tmp_path / "apps").resolve()
    root.mkdir()
    destination = root / "gomoku"
    app_id = "hub-review"
    ops = tmp_path / "market" / "install_ops"
    operation = hub_install._prepare_operation(ops, "job", app_id, root, destination, "install")
    destination.mkdir()
    hub_install._mark_release(destination, operation["release_id"])
    manifest = AppManifest(
        app_id=app_id, name="gomoku", version="1.0.0", run_type="static",
        work_dir=str(destination), owner_id=OWNER, source="hub", artifact_sha256="a" * 64,
    )
    manager.storage.save_manifest(app_id, manifest)
    operation["record"] = {
        "owner_id": OWNER, "release_id": operation["release_id"],
        "version": manifest.version, "artifact_sha256": manifest.artifact_sha256,
        "work_dir": str(destination),
    }
    hub_install._write_operation(ops, operation)

    assert len(hub_install.recover_interrupted_operations(ops, manager.storage)) == 1
    assert not destination.exists()
    assert manager.storage.load_manifest(app_id) is None
    assert not (ops / "job.json").exists()


def test_recovery_rolls_back_same_version_with_a_new_digest(tmp_path):
    manager = _manager(tmp_path)
    root = (tmp_path / "apps").resolve()
    root.mkdir()
    destination = root / "gomoku"
    destination.mkdir()
    old_file = destination / "edition.txt"
    old_file.write_text("original", encoding="utf-8")
    old_identity = hub_install._directory_identity(destination)
    ops = tmp_path / "market" / "install_ops"
    app_id = "hub-review"
    operation = hub_install._prepare_operation(ops, "job", app_id, root, destination, "update")
    old = AppManifest(
        app_id=app_id, name="gomoku", version="1.0.0", run_type="static",
        work_dir=str(destination), owner_id=OWNER, source="hub", artifact_sha256="a" * 64,
    )
    old_record = {
        "owner_id": OWNER, "release_id": "a" * 32, "version": old.version,
        "artifact_sha256": old.artifact_sha256, "work_dir": str(destination),
    }
    archive = root / ".app-archive"
    archive.mkdir()
    backup = archive / "gomoku-previous"
    new_record = {
        **old_record, "release_id": operation["release_id"], "artifact_sha256": "b" * 64,
    }
    operation.update(
        old_manifest=asdict(old), old_record=old_record,
        old_identity=old_identity, record=new_record, backup=str(backup),
    )
    hub_install._write_operation(ops, operation)

    destination.rename(backup)
    destination.mkdir()
    hub_install._mark_release(destination, operation["release_id"])
    manager.storage.save_install_record(app_id, old_record)
    manager.storage.save_manifest(app_id, AppManifest(**{**asdict(old), "artifact_sha256": "b" * 64}))

    assert len(hub_install.recover_interrupted_operations(ops, manager.storage)) == 1
    assert old_file.read_text(encoding="utf-8") == "original"
    assert manager.storage.load_manifest(app_id) == old
    assert manager.storage.load_install_record(app_id) == old_record


def test_update_credential_write_failure_restores_old_release(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    archive = _app_zip("1.0.0", "old")
    result = asyncio.run(hub_install.install_hub_app(
        manager=manager, asset_id=ASSET, install_root=tmp_path / "apps", owner_id=OWNER,
        hub_port=FakePort(archive, version="1.0.0"), downloader=_downloader(archive),
    ))
    app_id = result["app_id"]
    original = manager.storage.load_manifest(app_id)
    original_record = manager.storage.load_install_record(app_id)
    destination = Path(result["work_dir"])
    replacement = _app_zip("1.0.0", "replacement")
    new_port = FakePort(replacement, version="1.0.0")

    def fail_write(*args):
        raise OSError("cannot save install record")

    monkeypatch.setattr(manager.storage, "save_install_record", fail_write)
    with pytest.raises(OSError, match="cannot save install record"):
        asyncio.run(hub_install.update_hub_app(
            manager=manager, app_id=app_id, owner_id=OWNER, hub_port=new_port,
            downloader=_downloader(replacement), operation_dir=tmp_path / "market" / "install_ops",
        ))
    assert "old" in (destination / "assets" / "app.js").read_text(encoding="utf-8")
    assert manager.storage.load_manifest(app_id) == original
    assert manager.storage.load_install_record(app_id) == original_record


def test_uninstall_conflicts_with_an_update_job(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    app_id = "hub-review"
    manager.storage.save_manifest(app_id, AppManifest(
        app_id=app_id, name="gomoku", version="1.0.0", run_type="static",
        owner_id=OWNER, source="hub",
    ))

    async def exercise():
        started = asyncio.Event()
        release = asyncio.Event()

        async def update(**options):
            started.set()
            await release.wait()
            return {"updated": True}

        monkeypatch.setattr(hub_install, "update_hub_app", update)
        monkeypatch.setattr(manager, "_hub_port", lambda: None)
        job = await manager.start_update(app_id=app_id, owner_id=OWNER)
        await started.wait()
        try:
            with pytest.raises(AppMarketError) as busy:
                await manager.uninstall_app(app_id, OWNER)
            assert busy.value.code == "app_operation_conflict"
            assert manager.storage.load_manifest(app_id) is not None
        finally:
            release.set()
            await manager.shutdown()
        assert manager.install_status(job["job_id"], OWNER)["state"] == "done"

    asyncio.run(exercise())


def test_hub_server_start_policy_and_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("APP_MARKET_ALLOW_HUB_CODE", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    manager = _manager(tmp_path)
    manager.configure_gateway(app_host="0.0.0.0")
    scripts = tmp_path / "app" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "app_server.py").write_text("print('ready')", encoding="utf-8")
    manifest = AppManifest(
        app_id="hub-review", name="gomoku", version="1.0.0", run_type="static",
        work_dir=str(scripts.parent), owner_id=OWNER, source="hub", artifact_sha256="a" * 64,
    )
    manager.storage.save_manifest(manifest.app_id, manifest)

    with pytest.raises(AppMarketError) as disabled:
        asyncio.run(manager.start_app(manifest.app_id, OWNER))
    assert disabled.value.code == "app_run_disabled"
    manager.configure_gateway(app_host="127.0.0.1")
    with pytest.raises(AppMarketError) as incomplete:
        asyncio.run(manager.start_app(manifest.app_id, OWNER))
    assert incomplete.value.code == "app_operation_conflict"
    manager.storage.save_install_record(manifest.app_id, {
        "owner_id": OWNER, "work_dir": manifest.work_dir, "version": manifest.version,
        "artifact_sha256": manifest.artifact_sha256,
    })
    with pytest.raises(RunConsentRequired):
        asyncio.run(manager.start_app(manifest.app_id, OWNER))
    assert "OPENAI_API_KEY" not in _application_env(manifest)
    monkeypatch.setenv("APPDATA", "C:/Users/test/AppData/Roaming")
    assert _application_env(manifest)["APPDATA"] == "C:/Users/test/AppData/Roaming"
    manifest.source = "local"
    assert _application_env(manifest)["OPENAI_API_KEY"] == "test-only"


def test_list_apps_flags_missing_work_dir(tmp_path):
    manager = _manager(tmp_path)
    app_dir = tmp_path / "appdir"
    app_dir.mkdir()
    source = app_dir / "page.html"
    source.write_text("<html></html>", encoding="utf-8")
    created = asyncio.run(manager.publish_app(str(source), "source", OWNER))
    manifest = manager.service.get_app(created["app_id"])
    shutil.rmtree(manifest.work_dir)

    apps = manager.list_apps(OWNER)["apps"]

    assert apps[0]["work_dir_missing"] is True


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_uninstall_keeps_files_by_default_and_archives_on_cleanup(tmp_path, monkeypatch, cleanup_fails):
    payload = _app_zip()
    manager = _manager(tmp_path)
    port = FakePort(payload)
    result = asyncio.run(
        hub_install.install_hub_app(
            manager=manager,
            asset_id=ASSET,
            install_root=tmp_path / "apps",
            owner_id=OWNER,
            hub_port=port,
            downloader=_downloader(payload),
        )
    )
    app_id = result["app_id"]
    destination = Path(result["work_dir"])
    (destination / "data").mkdir()
    (destination / "data" / "save.json").write_text("{}", encoding="utf-8")

    kept = asyncio.run(
        hub_install.uninstall_hub_app(
            manager=manager, app_id=app_id, owner_id=OWNER, remove_files=False
        )
    )
    assert kept["removed_files"] is False
    assert destination.is_dir()
    assert manager.service.get_app(app_id) is None

    # Reinstall into a clean root, then explicit cleanup archives user data.
    result = asyncio.run(
        hub_install.install_hub_app(
            manager=manager,
            asset_id=ASSET,
            install_root=tmp_path / "apps2",
            owner_id=OWNER,
            hub_port=port,
            downloader=_downloader(payload),
        )
    )
    destination = Path(result["work_dir"])
    (destination / "data").mkdir()
    (destination / "data" / "save.json").write_text("{}", encoding="utf-8")
    if cleanup_fails:
        def remove_locked_app(path):
            assert path == destination
            assert manager.service.get_app(app_id) is None
            error = PermissionError("Application files are locked")
            error.winerror = 32
            raise error

        monkeypatch.setattr(hub_install, "_remove_tree", remove_locked_app)

    removed = asyncio.run(
        manager.uninstall_app(app_id, OWNER, remove_files=True)
    )
    assert removed["removed_files"] is (not cleanup_fails)
    assert destination.exists() is cleanup_fails
    assert removed["work_dir"] == str(destination)
    assert removed["cleanup_warning"] == ("Application files are locked" if cleanup_fails else "")
    assert removed["cleanup_code"] == ("filesystem_busy" if cleanup_fails else "")
    archived = Path(removed["preserved_dir"])
    assert (archived / "data" / "save.json").is_file()
    assert manager.service.get_app(app_id) is None
    assert manager.storage.load_install_record(app_id) is None


def test_uninstall_keeps_the_directory_when_deregistration_fails(tmp_path, monkeypatch):
    """A failed deregistration must not leave files deleted behind a manifest."""
    payload = _app_zip()
    manager = _manager(tmp_path)
    port = FakePort(payload)
    result = asyncio.run(
        hub_install.install_hub_app(
            manager=manager,
            asset_id=ASSET,
            install_root=tmp_path / "apps",
            owner_id=OWNER,
            hub_port=port,
            downloader=_downloader(payload),
        )
    )
    destination = Path(result["work_dir"])

    async def refuse_delete(app_id: str) -> bool:
        return False

    monkeypatch.setattr(manager.service, "delete_app", refuse_delete)
    with pytest.raises(hub_install.AppInstallError) as error:
        asyncio.run(
            hub_install.uninstall_hub_app(
                manager=manager,
                app_id=result["app_id"],
                owner_id=OWNER,
                remove_files=True,
            )
        )

    assert error.value.code == "app_operation_conflict"
    assert destination.is_dir()
    assert manager.service.get_app(result["app_id"]) is not None


def test_market_list_and_detail_report_a_missing_working_directory(tmp_path, monkeypatch):
    payload = _app_zip()
    manager = _manager(tmp_path)
    port = FakePort(payload)
    monkeypatch.setattr(manager, "_hub_endpoint", lambda: (port, HUB_URL))
    installed = asyncio.run(
        hub_install.install_hub_app(
            manager=manager,
            asset_id=ASSET,
            install_root=tmp_path / "apps",
            owner_id=OWNER,
            hub_port=port,
            downloader=_downloader(payload),
        )
    )

    listing = asyncio.run(manager.market_list(owner_id=OWNER))
    item = listing["items"][0]
    assert item["installed"] is True
    assert item["work_dir_missing"] is False

    shutil.rmtree(installed["work_dir"])
    listing = asyncio.run(manager.market_list(owner_id=OWNER))
    item = listing["items"][0]
    assert item["installed"] is True
    assert item["work_dir_missing"] is True

    detail = asyncio.run(manager.market_detail(asset_id=ASSET, owner_id=OWNER))
    assert detail["installed"] is True
    assert detail["installed_work_dir_missing"] is True


def _zip_with(paths: dict[str, str]) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, content in paths.items():
            archive.writestr(name, content)
    return stream.getvalue()


def test_app_archive_rejects_paths_windows_cannot_install() -> None:
    base = {"gomoku/plugin.yaml": "name: gomoku\n", "gomoku/gomoku/index.html": "ok"}
    cases = {
        "reserved-device": {**base, "gomoku/gomoku/assets/aux.txt": "x"},
        "case-fold-collision": {**base, "gomoku/gomoku/assets/A.txt": "a", "gomoku/gomoku/assets/a.txt": "b"},
        "trailing-dot": {**base, "gomoku/gomoku/assets/name.": "x"},
        "trailing-space": {**base, "gomoku/gomoku/assets/name ": "x"},
        "illegal-char": {**base, "gomoku/gomoku/assets/a:b.txt": "x"},
    }
    for label, paths in cases.items():
        with pytest.raises(hub_install.AppInstallError) as exc_info:
            hub_install.validate_app_archive_paths(_zip_with(paths))
        assert exc_info.value.code == "invalid_plugin_structure", label


def test_app_archive_accepts_portable_paths() -> None:
    hub_install.validate_app_archive_paths(
        _zip_with({
            "gomoku/plugin.yaml": "name: gomoku\n",
            "gomoku/gomoku/index.html": "ok",
            "gomoku/gomoku/assets/app.js": "1",
            "gomoku/gomoku/scripts/app_server.py": "print('ok')\n",
        })
    )


def test_empty_owner_is_local_single_user_identity(tmp_path, monkeypatch):
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)
    payload = _app_zip()
    manager = _manager(tmp_path)
    result = asyncio.run(
        hub_install.install_hub_app(
            manager=manager,
            asset_id=ASSET,
            version="1.2.3",
            install_root=tmp_path / "apps",
            owner_id="",
            hub_port=FakePort(payload),
            downloader=_downloader(payload),
        )
    )
    assert result["app_id"].startswith("hub-")
    record = manager.storage.load_install_record(result["app_id"])
    assert record["owner_id"] == ""


def test_local_delete_keep_files_only_unregisters(tmp_path):
    manager = _manager(tmp_path)
    source = tmp_path / "workspace" / "demo"
    source.mkdir(parents=True)
    (source / "index.html").write_text("<html></html>", encoding="utf-8")
    published = asyncio.run(manager.publish_app(str(source / "index.html"), "session", OWNER))
    app_id = published["app_id"]
    manifest = manager.service.get_app(app_id)
    manifest.project_id = "proj_keep"
    manifest.project_dir = str(tmp_path / "project")
    manifest.project_work_mode = "code"
    manager.storage.save_manifest(app_id, manifest)

    result = asyncio.run(manager.delete_app(app_id, OWNER, remove_files=False))
    assert result["deleted"] is True
    assert result["removed_files"] is False
    assert result["project_retained"] is True
    assert source.is_dir()
    assert manager.service.get_app(app_id) is None


def test_local_delete_clean_removes_app_owned_dir_and_archives_data(tmp_path, monkeypatch):
    install_root = tmp_path / "apps"
    monkeypatch.setenv("APP_MARKET_INSTALL_ROOT", str(install_root))
    manager = _manager(tmp_path)
    source = install_root / "demo"
    source.mkdir(parents=True)
    (source / "index.html").write_text("<html></html>", encoding="utf-8")
    (source / "data").mkdir()
    (source / "data" / "save.json").write_text("{}", encoding="utf-8")
    published = asyncio.run(manager.publish_app(str(source / "index.html"), "session", OWNER))
    app_id = published["app_id"]
    manifest = manager.service.get_app(app_id)
    manifest.project_id = "proj_keep"
    manifest.project_dir = str(tmp_path / "project")
    manifest.project_work_mode = "code"
    manager.storage.save_manifest(app_id, manifest)

    result = asyncio.run(
        manager.delete_app(app_id, OWNER, remove_files=True, confirm_work_dir=str(source))
    )
    assert result["deleted"] is True
    assert result["removed_files"] is True
    assert result["project_retained"] is True
    assert not source.exists()
    preserved = Path(result["preserved_dir"])
    assert (preserved / "data" / "save.json").is_file()
    assert manager.service.get_app(app_id) is None


def test_local_delete_clean_removes_user_source_directory(tmp_path):
    manager = _manager(tmp_path)
    source = tmp_path / "workspace" / "demo"
    source.mkdir(parents=True)
    (source / "index.html").write_text("<html></html>", encoding="utf-8")
    (source / ".workswarm-app-release").write_text("unverified", encoding="ascii")
    published = asyncio.run(manager.publish_app(str(source / "index.html"), "session", OWNER))
    app_id = published["app_id"]

    result = asyncio.run(
        manager.delete_app(app_id, OWNER, remove_files=True, confirm_work_dir=str(source))
    )
    assert result["deleted"] is True
    assert result["removed_files"] is True
    assert result["cleanup_warning"] == ""
    assert not source.exists()
    assert manager.service.get_app(app_id) is None


def test_local_delete_clean_removes_read_only_files(tmp_path):
    manager = _manager(tmp_path)
    source = tmp_path / "workspace" / "demo"
    (source / "data").mkdir(parents=True)
    (source / "index.html").write_text("<html></html>", encoding="utf-8")
    locked = source / "data" / "save.json"
    locked.write_text("{}", encoding="utf-8")
    os.chmod(locked, stat.S_IREAD)
    published = asyncio.run(manager.publish_app(str(source / "index.html"), "session", OWNER))

    result = asyncio.run(
        manager.delete_app(published["app_id"], OWNER, remove_files=True, confirm_work_dir=str(source))
    )

    assert result["removed_files"] is True
    assert result["cleanup_warning"] == ""
    assert not source.exists()
    assert (Path(result["preserved_dir"]) / "data" / "save.json").is_file()


def test_local_delete_clean_does_not_follow_directory_links(tmp_path):
    manager = _manager(tmp_path)
    source = tmp_path / "workspace" / "demo"
    source.mkdir(parents=True)
    (source / "index.html").write_text("<html></html>", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    if os.name == "nt":
        linked = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(source / "linked"), str(outside)],
            capture_output=True,
        )
        if linked.returncode != 0:
            pytest.skip("directory junctions are unavailable")
    else:
        try:
            os.symlink(outside, source / "linked", target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"directory links are unavailable: {exc}")
    published = asyncio.run(manager.publish_app(str(source / "index.html"), "session", OWNER))

    result = asyncio.run(
        manager.delete_app(published["app_id"], OWNER, remove_files=True, confirm_work_dir=str(source))
    )

    assert result["removed_files"] is True
    assert not source.exists()
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize(
    "app_path,other_entry,registered,cleanup_code",
    [
        (".", "other/index.html", False, "protected_directory"),
        ("demo", "demo/nested/index.html", True, "directory_in_use"),
        ("demo", "demo/other.html", True, "directory_in_use"),
    ],
)
def test_local_delete_clean_protects_other_files(
    tmp_path, monkeypatch, app_path, other_entry, registered, cleanup_code
):
    install_root = tmp_path / "apps"
    monkeypatch.setenv("APP_MARKET_INSTALL_ROOT", str(install_root))
    manager = _manager(tmp_path)
    entry = install_root / app_path / "index.html"
    other = install_root / other_entry
    other.parent.mkdir(parents=True)
    for page in (entry, other):
        page.write_text("<html></html>", encoding="utf-8")
    app = asyncio.run(manager.publish_app(str(entry), "", OWNER))
    other_app = asyncio.run(manager.publish_app(str(other), "", "bob")) if registered else None

    result = asyncio.run(
        manager.delete_app(app["app_id"], OWNER, remove_files=True, confirm_work_dir=str(entry.parent))
    )
    assert result["deleted"] is True
    assert result["removed_files"] is False
    assert result["cleanup_code"] == cleanup_code
    assert result["cleanup_warning"]
    assert entry.is_file() and other.is_file()
    assert manager.service.get_app(app["app_id"]) is None
    if other_app is not None:
        assert manager.service.get_app(other_app["app_id"]) is not None


def test_overlap_check_uses_canonical_paths(tmp_path):
    manager = _manager(tmp_path)
    app_dir = tmp_path / "apps" / "demo"
    app_dir.mkdir(parents=True)
    manager.storage.save_manifest("other-app", AppManifest(
        app_id="other-app",
        name="Other",
        version="1.0.0",
        run_type="static",
        work_dir=str(app_dir / ".." / "demo"),
        owner_id=OWNER,
    ))

    assert manager.storage.has_overlapping_work_dir(app_dir) is True
    assert manager.storage.has_overlapping_work_dir(app_dir / "nested") is True
    assert manager.storage.has_overlapping_work_dir(tmp_path / "elsewhere") is False


def test_overlap_check_treats_unresolvable_input_as_overlap(tmp_path):
    manager = _manager(tmp_path)
    assert manager.storage.has_overlapping_work_dir(Path("bad\0path")) is True


def test_local_delete_clean_keeps_files_when_deregistration_fails(tmp_path, monkeypatch):
    install_root = tmp_path / "apps"
    monkeypatch.setenv("APP_MARKET_INSTALL_ROOT", str(install_root))
    manager = _manager(tmp_path)
    entry = install_root / "demo" / "index.html"
    entry.parent.mkdir(parents=True)
    entry.write_text("<html></html>", encoding="utf-8")
    app = asyncio.run(manager.publish_app(str(entry), "", OWNER))
    registration = manager.storage.install_record_path(app["app_id"]).parent
    rmtree = shutil.rmtree

    def fail_registry_removal(path, *args, **kwargs):
        if path == registration:
            raise PermissionError("Registry directory is locked")
        return rmtree(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", fail_registry_removal)
    result = asyncio.run(
        manager.delete_app(app["app_id"], OWNER, remove_files=True, confirm_work_dir=str(entry.parent))
    )
    assert result == {"deleted": False}
    assert entry.is_file()
    assert manager.service.get_app(app["app_id"]) is not None


def test_local_delete_clean_requires_directory_confirmation(tmp_path):
    manager = _manager(tmp_path)
    source = tmp_path / "workspace" / "demo"
    source.mkdir(parents=True)
    (source / "index.html").write_text("<html></html>", encoding="utf-8")
    published = asyncio.run(manager.publish_app(str(source / "index.html"), "session", OWNER))

    for confirmation in ("", str(tmp_path / "workspace"), str(source / "index.html")):
        with pytest.raises(AppMarketError) as error:
            asyncio.run(
                manager.delete_app(published["app_id"], OWNER, remove_files=True, confirm_work_dir=confirmation)
            )
        assert error.value.code == "app_delete_confirm_required"

    assert source.is_dir() and (source / "index.html").is_file()
    assert manager.service.get_app(published["app_id"]) is not None


def test_local_delete_clean_keeps_the_current_working_directory(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    source = tmp_path / "workspace" / "demo"
    source.mkdir(parents=True)
    (source / "index.html").write_text("<html></html>", encoding="utf-8")
    published = asyncio.run(manager.publish_app(str(source / "index.html"), "session", OWNER))
    monkeypatch.chdir(source)

    result = asyncio.run(
        manager.delete_app(published["app_id"], OWNER, remove_files=True, confirm_work_dir=str(source))
    )

    assert result["deleted"] is True
    assert result["removed_files"] is False
    assert result["cleanup_code"] == "protected_directory"
    assert (source / "index.html").is_file()


@pytest.mark.parametrize("path", [Path.home(), Path(Path.cwd().anchor)])
def test_protected_directory_guard_rejects_roots_and_home(path):
    assert hub_install._protected_directory_reason(path)
    assert hub_install._protected_directory_reason(Path.home() / "wrong") == ""


@pytest.mark.parametrize(
    "error_number,cleanup_code",
    [(errno.EBUSY, "filesystem_busy"), (errno.EACCES, "filesystem_error")],
)
def test_local_delete_deregisters_even_if_cleanup_fails(tmp_path, monkeypatch, error_number, cleanup_code):
    manager = _manager(tmp_path)
    source = tmp_path / "workspace" / "demo"
    (source / "data").mkdir(parents=True)
    (source / "index.html").write_text("<html></html>", encoding="utf-8")
    (source / "data" / "save.json").write_text("{}", encoding="utf-8")
    published = asyncio.run(manager.publish_app(str(source / "index.html"), "session", OWNER))
    app_id = published["app_id"]

    def remove_failed(path):
        assert path == source
        assert manager.service.get_app(app_id) is None
        raise OSError(error_number, "Could not remove application directory")

    monkeypatch.setattr(hub_install, "_remove_tree", remove_failed)
    result = asyncio.run(
        manager.delete_app(app_id, OWNER, remove_files=True, confirm_work_dir=str(source))
    )
    assert result["deleted"] is True
    assert result["removed_files"] is False
    assert result["cleanup_code"] == cleanup_code
    assert result["work_dir"] == str(source)
    assert (source / "index.html").is_file()
    assert (Path(result["preserved_dir"]) / "data" / "save.json").is_file()
