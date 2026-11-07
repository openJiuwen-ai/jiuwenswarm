"""plugin_doctor 与宿主真实加载链路的契约测试。

这些用例**不 mock loader**：直接跑 ``ExtensionLoader`` + ``ExtensionRegistry`` + FastAPI 资产路由，
把引擎里「宿主会这样处理」的断言钉在真实实现上——引擎说 PD007/PD008/PD011/PD016 会让插件加载失败，
这里就用真 loader 复现一次失败。
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jiuwenswarm.gateway.extensions.application_host import (
    application_plugin_manifest,
    mount_application_plugin_http_routes,
)
from jiuwenswarm.extensions.loader import ExtensionLoader, _is_extension_root
from jiuwenswarm.extensions.plugin_doctor import doctor_engine
from jiuwenswarm.extensions.plugin_doctor import extension as plugin_module
from jiuwenswarm.extensions.registry import ExtensionRegistry

PLUGIN_ROOT = Path(plugin_module.__file__).resolve().parent
EXTENSIONS_DIR = PLUGIN_ROOT.parent
FRONTEND_PAGE = PLUGIN_ROOT / "frontend" / "dist" / "index.html"


class _FakeChannel:
    def __init__(self) -> None:
        self.methods: dict[str, Any] = {}
        self.local_only: set[str] = set()

    def register_method(self, method, handler, *, local_only=False) -> None:  # noqa: ANN001
        self.methods[method] = handler
        if local_only:
            self.local_only.add(method)

    async def send_response(self, ws, req_id, **payload) -> None:  # noqa: ANN001
        ws.append({"id": req_id, **payload})


def make_registry() -> ExtensionRegistry:
    return ExtensionRegistry(MagicMock(), {}, MagicMock())


def write_manifest_only(root: Path, manifest: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "extension.yaml").write_text(manifest, encoding="utf-8")
    return root


# --------------------------------------------------------------------------
# 发现与加载
# --------------------------------------------------------------------------


def test_discovery_finds_plugin_doctor_next_to_builtins() -> None:
    loader = ExtensionLoader(make_registry())
    loader.add_search_path(EXTENSIONS_DIR)

    discovered = {root.name for root in loader.discover_extension_roots()}

    assert {"plugin_doctor", "agentos", "agent_client", "video_duplex"} <= discovered


class _BlockedImport:
    """拦截指定模块的导入，用来模拟「插件目录不在 jiuwenswarm 包内」的形态。"""

    def __init__(self, name: str) -> None:
        self.name = name

    def find_spec(self, fullname, path=None, target=None):  # noqa: ANN001
        if fullname == self.name:
            raise ImportError(f"blocked: {fullname}")
        return None


def test_third_party_layout_loads_the_engine_without_touching_sys_path(
    tmp_path: Path,
) -> None:
    """第三方目录形态：按文件路径加载同级引擎，且**不修改 sys.path**（CodeCheck G.PSL.03）。"""

    probe = tmp_path / "plugin_doctor_probe"
    probe.mkdir()
    for name in ("extension.py", "doctor_engine.py"):
        shutil.copy2(PLUGIN_ROOT / name, probe / name)

    saved = sys.modules.pop("jiuwenswarm.extensions.plugin_doctor", None)
    blocked = _BlockedImport("jiuwenswarm.extensions.plugin_doctor")
    sys.meta_path.insert(0, blocked)
    path_before = list(sys.path)
    try:
        module = ExtensionLoader._import_module(probe)
    finally:
        sys.meta_path.remove(blocked)
        sys.modules.pop("plugin_doctor_engine", None)
        if saved is not None:
            sys.modules["jiuwenswarm.extensions.plugin_doctor"] = saved

    assert Path(module.doctor_engine.__file__).resolve().parent == probe.resolve()
    assert module.doctor_engine.ENTRY_FILENAME == "extension.py"
    assert sys.path == path_before


def test_engine_and_loader_agree_on_extension_root_detection() -> None:
    for subdir in EXTENSIONS_DIR.iterdir():
        assert doctor_engine.is_extension_root(subdir) is _is_extension_root(subdir)


def test_manifest_is_readable_without_importing_the_entry() -> None:
    manifest = ExtensionLoader.load_manifest(PLUGIN_ROOT)

    assert manifest["id"] == "plugin-doctor"
    assert manifest["package_type"] == "application"
    assert manifest["version"] == "1.0.0"


@pytest.mark.asyncio
async def test_real_loader_registers_the_plugin() -> None:
    registry = make_registry()

    loaded = await ExtensionLoader(registry).load_extension(PLUGIN_ROOT)

    assert loaded
    plugin = registry.get_application_plugin("plugin-doctor")
    assert plugin is not None
    assert plugin.plugin_id == "plugin-doctor"
    assert plugin.metadata.id == "plugin-doctor"
    assert plugin.frontend_asset_root() == PLUGIN_ROOT / "frontend" / "dist"
    assert plugin.is_enabled() is True


@pytest.mark.asyncio
async def test_manifest_only_plugin_without_frontend_fails_like_pd007(tmp_path: Path) -> None:
    root = write_manifest_only(
        tmp_path / "bare",
        "id: bare\nname: Bare\nversion: 1.0.0\ndescription: d\nauthor: a\n"
        "package_type: application\n",
    )

    with pytest.raises(ValueError, match="at least one frontend"):
        await ExtensionLoader(make_registry()).load_extension(root)


@pytest.mark.asyncio
async def test_manifest_only_plugin_without_id_fails_like_pd006(tmp_path: Path) -> None:
    root = write_manifest_only(
        tmp_path / "no-id",
        "name: NoId\nversion: 1.0.0\ndescription: d\nauthor: a\n"
        "package_type: application\nfrontend:\n  - id: p\n    entrypoint: index.html\n",
    )
    (root / "frontend" / "dist").mkdir(parents=True)
    (root / "frontend" / "dist" / "index.html").write_text("<html></html>", encoding="utf-8")

    with pytest.raises(ValueError, match="id must not be empty"):
        await ExtensionLoader(make_registry()).load_extension(root)


@pytest.mark.asyncio
async def test_manifest_only_plugin_with_bad_render_mode_fails_like_pd008(
    tmp_path: Path,
) -> None:
    root = write_manifest_only(
        tmp_path / "bundled",
        "id: bundled\nname: B\nversion: 1.0.0\ndescription: d\nauthor: a\n"
        "package_type: application\nfrontend:\n  - id: p\n    render_mode: bundled\n",
    )

    with pytest.raises(ValueError):
        await ExtensionLoader(make_registry()).load_extension(root)


@pytest.mark.asyncio
async def test_extension_without_entry_and_without_application_type_fails_like_pd011(
    tmp_path: Path,
) -> None:
    root = write_manifest_only(
        tmp_path / "legacy",
        "id: legacy\nname: Legacy\nversion: 1.0.0\ndescription: d\nauthor: a\n",
    )

    with pytest.raises(FileNotFoundError):
        await ExtensionLoader(make_registry()).load_extension(root)


ENTRY_WITHOUT_MANIFEST = '''\
from jiuwenswarm.extensions.sdk import ApplicationPluginExtension


class NoManifestPlugin(ApplicationPluginExtension):
    plugin_id = "no-manifest"

    async def initialize(self, config):
        return None

    async def shutdown(self):
        return None


async def register_extensions(registry):
    plugin = NoManifestPlugin()
    registry.register_application_plugin(plugin)
    return [plugin]
'''


@pytest.mark.asyncio
async def test_missing_manifest_breaks_metadata_like_pd031(tmp_path: Path) -> None:
    """PD031（error）：插件本身能加载，但任何元数据读取都会抛 FileNotFoundError。"""

    root = tmp_path / "no-manifest"
    root.mkdir()
    (root / "extension.py").write_text(ENTRY_WITHOUT_MANIFEST, encoding="utf-8")
    registry = make_registry()

    loaded = await ExtensionLoader(registry).load_extension(root)

    assert loaded
    plugin = registry.get_application_plugin("no-manifest")
    assert plugin is not None
    with pytest.raises(FileNotFoundError):
        _ = plugin.metadata
    with pytest.raises(FileNotFoundError):
        application_plugin_manifest(registry)


def test_duplicate_plugin_id_is_rejected_like_pd016() -> None:
    registry = make_registry()
    plugin = plugin_module.PluginDoctorApplicationPlugin()
    registry.register_application_plugin(plugin)

    with pytest.raises(ValueError, match="already registered"):
        registry.register_application_plugin(plugin_module.PluginDoctorApplicationPlugin())


def test_plugin_directory_name_is_a_valid_module_name() -> None:
    # loader 用 jiuwenswarm.loaded_extension.<目录名> 作为模块名（loader.py:154-158）
    assert PLUGIN_ROOT.name.isidentifier()


# --------------------------------------------------------------------------
# RPC 端到端
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_report_rpc_over_the_real_extensions_dir_has_no_errors() -> None:
    registry = make_registry()
    loaded = await ExtensionLoader(registry).load_extension(PLUGIN_ROOT)
    channel = _FakeChannel()
    registry.bind_application_plugins(channel)
    responses: list[dict[str, Any]] = []
    del loaded

    await channel.methods["plugin.doctor.report"](
        responses, "req-1", {"search_paths": [str(EXTENSIONS_DIR)]}, "session-1"
    )

    payload = responses[-1]["payload"]
    assert responses[-1]["ok"] is True
    assert payload["errors"] == 0
    assert payload["scanned"] >= 4
    scanned = {plugin["plugin_id"] for plugin in payload["plugins"]}
    assert {"plugin-doctor", "agentos_router", "agent_client", "video-duplex"} <= scanned


@pytest.mark.asyncio
async def test_export_rpc_returns_reproducible_markdown() -> None:
    registry = make_registry()
    await ExtensionLoader(registry).load_extension(PLUGIN_ROOT)
    channel = _FakeChannel()
    registry.bind_application_plugins(channel)
    responses: list[dict[str, Any]] = []

    await channel.methods["plugin.doctor.report"](
        responses, "req-1", {"search_paths": [str(EXTENSIONS_DIR)]}, "session-1"
    )
    await channel.methods["plugin.doctor.export"](
        responses, "req-2", {"format": "markdown"}, "session-1"
    )

    markdown = responses[-1]["payload"]["content"]
    assert markdown.startswith("# 应用插件体检报告")
    assert "plugin-doctor" in markdown
    assert "PD" in markdown


# --------------------------------------------------------------------------
# 宿主 HTTP 面：清单与 iframe 资产路由
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manifest_endpoint_exposes_iframe_page() -> None:
    registry = make_registry()
    await ExtensionLoader(registry).load_extension(PLUGIN_ROOT)

    manifest = application_plugin_manifest(registry)
    entry = next(
        item for item in manifest["plugins"] if item["plugin_id"] == "plugin-doctor"
    )

    assert manifest["api_version"] == 1
    assert entry["render_mode"] == "iframe"
    assert entry["nav_key"] == "app:plugin-doctor"
    assert entry["entry_url"].endswith("/plugin-doctor/assets/index.html")
    assert entry["enabled"] is True


@pytest.mark.asyncio
async def test_asset_route_serves_the_page_and_blocks_traversal() -> None:
    registry = make_registry()
    await ExtensionLoader(registry).load_extension(PLUGIN_ROOT)
    app = FastAPI()
    mount_application_plugin_http_routes(app, registry)
    client = TestClient(app)

    page = client.get("/api/application-plugins/plugin-doctor/assets/index.html")
    traversal = client.get(
        "/api/application-plugins/plugin-doctor/assets/%2e%2e%2fextension.py"
    )
    unknown = client.get("/api/application-plugins/no-such/assets/index.html")

    assert page.status_code == 200
    assert 'data-testid="plugin-doctor-root"' in page.text
    assert traversal.status_code == 404
    assert unknown.status_code == 404


def test_frontend_page_calls_every_registered_rpc_method() -> None:
    html = FRONTEND_PAGE.read_text(encoding="utf-8")

    assert "PREFIX = 'plugin.doctor.'" in html
    for method in ("paths", "rules", "report", "inspect", "export"):
        assert f"call('{method}'" in html


def test_frontend_page_is_offline_and_self_contained() -> None:
    html = FRONTEND_PAGE.read_text(encoding="utf-8")

    assert "<script src=" not in html
    assert "<link rel=\"stylesheet\"" not in html
    assert "http://" not in html
    assert "https://" not in html


def test_frontend_page_data_testids_are_namespaced() -> None:
    html = FRONTEND_PAGE.read_text(encoding="utf-8")

    testids = {
        part.split('"')[0]
        for part in html.split('data-testid="')[1:]
    }
    assert testids
    assert all(name.startswith("plugin-doctor-") for name in testids)


def test_pyproject_ships_plugin_frontend_assets() -> None:
    """iframe 页面必须随 wheel 发布，否则 entry_url 在生产包里 404。"""

    pyproject = PLUGIN_ROOT.parents[2] / "pyproject.toml"

    text = pyproject.read_text(encoding="utf-8")

    assert '"extensions/**/frontend/dist/**/*"' in text
    assert '"extensions/**/frontend/dist/*"' in text


def test_reported_json_is_serializable_from_the_engine(tmp_path: Path) -> None:
    report = doctor_engine.scan_search_paths([EXTENSIONS_DIR], running_version="0.2.5")

    payload = json.loads(json.dumps(report.to_dict(), ensure_ascii=False))

    assert payload["scanned"] >= 4
    assert payload["score"] <= 100
