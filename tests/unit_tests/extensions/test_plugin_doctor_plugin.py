"""plugin_doctor 插件层单元测试：RPC 契约、参数校验、路径限制、导出。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from jiuwenswarm.extensions.plugin_doctor import doctor_engine
from jiuwenswarm.extensions.plugin_doctor import extension as plugin_module
from jiuwenswarm.extensions.plugin_doctor.extension import (
    RPC_PREFIX,
    PluginDoctorApplicationPlugin,
    register_extensions,
)
from jiuwenswarm.extensions.registry import ExtensionRegistry

HEALTHY_MANIFEST = """\
id: good-plugin
name: Good Plugin
version: 1.2.3
description: healthy
author: tests
package_type: application
frontend:
  - id: good-page
    title: Good
    entrypoint: index.html
"""

BROKEN_MANIFEST = "id: broken-plugin\nname: [unclosed\n"


class _FakeChannel:
    """与宿主 tests/unit_tests/test_application_plugins.py 里的假通道保持一致。"""

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


def bind(plugin: PluginDoctorApplicationPlugin) -> tuple[_FakeChannel, list[dict[str, Any]]]:
    registry = make_registry()
    registry.register_application_plugin(plugin)
    channel = _FakeChannel()
    registry.bind_application_plugins(channel)
    return channel, []


def write_plugin(root: Path, name: str = "good-plugin", manifest: str = HEALTHY_MANIFEST) -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "extension.yaml").write_text(manifest, encoding="utf-8")
    assets = directory / "frontend" / "dist"
    assets.mkdir(parents=True, exist_ok=True)
    (assets / "index.html").write_text("<html>ok</html>", encoding="utf-8")
    return directory


async def call(
    channel: _FakeChannel,
    responses: list[dict[str, Any]],
    name: str,
    params: Any = None,
) -> dict[str, Any]:
    await channel.methods[RPC_PREFIX + name](responses, "req-1", params, "session-1")
    return responses[-1]


# --------------------------------------------------------------------------
# 注册与元数据
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_extensions_registers_application_plugin() -> None:
    registry = make_registry()

    loaded = await register_extensions(registry)

    assert len(loaded) == 1
    plugin = loaded[0]
    assert isinstance(plugin, PluginDoctorApplicationPlugin)
    assert plugin.plugin_id == "plugin-doctor"
    assert registry.get_application_plugin("plugin-doctor") is plugin


def test_metadata_matches_manifest() -> None:
    metadata = PluginDoctorApplicationPlugin().metadata

    assert metadata.id == "plugin-doctor"
    assert metadata.name
    assert metadata.version
    assert metadata.description
    assert metadata.author
    assert metadata.package_type == "application"


def test_frontend_contribution_is_iframe_page_with_existing_asset() -> None:
    plugin = PluginDoctorApplicationPlugin()

    contributions = plugin.frontend_contributions()

    assert len(contributions) == 1
    contribution = contributions[0]
    assert contribution.render_mode == "iframe"
    assert contribution.entrypoint == "index.html"
    assert contribution.nav_key == "app:plugin-doctor"
    assert contribution.id == "plugin-doctor-page"
    assets = plugin.frontend_asset_root()
    assert assets is not None
    assert (assets / "index.html").is_file()


def test_plugin_is_enabled_by_default() -> None:
    assert PluginDoctorApplicationPlugin().is_enabled() is True


@pytest.mark.asyncio
async def test_initialize_reads_config_and_logger() -> None:
    plugin = PluginDoctorApplicationPlugin()
    logger = object()

    class _Config:
        config = {"extensions": {"extension_dirs": "x"}}

    _Config.logger = logger  # type: ignore[attr-defined]

    await plugin.initialize(_Config())
    assert plugin._config == {"extensions": {"extension_dirs": "x"}}
    assert plugin._logger is logger

    await plugin.initialize(None)
    assert plugin._config == {}


@pytest.mark.asyncio
async def test_shutdown_clears_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = PluginDoctorApplicationPlugin()
    monkeypatch.setattr(plugin, "_user_plugin_dir", lambda: write_parent(tmp_path))
    channel, responses = bind(plugin)
    await call(channel, responses, "report")
    assert plugin._cached is not None

    await plugin.shutdown()

    assert plugin._cached is None


def write_parent(tmp_path: Path) -> Path:
    root = tmp_path / "application_plugins"
    root.mkdir(parents=True, exist_ok=True)
    return root


# --------------------------------------------------------------------------
# RPC 注册
# --------------------------------------------------------------------------


def test_bind_web_channel_registers_local_methods() -> None:
    channel, _ = bind(PluginDoctorApplicationPlugin())

    assert set(channel.methods) == {
        "plugin.doctor.paths",
        "plugin.doctor.rules",
        "plugin.doctor.report",
        "plugin.doctor.inspect",
        "plugin.doctor.export",
    }
    assert channel.local_only == set(channel.methods)


@pytest.mark.asyncio
async def test_disabled_plugin_rpc_is_gated_by_host() -> None:
    plugin = PluginDoctorApplicationPlugin()
    plugin.is_enabled = lambda: False  # type: ignore[method-assign]
    channel, responses = bind(plugin)

    response = await call(channel, responses, "report")

    assert response["ok"] is False
    assert response["code"] == "APPLICATION_PLUGIN_DISABLED"


# --------------------------------------------------------------------------
# plugin.doctor.paths / rules
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paths_handler_lists_search_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = PluginDoctorApplicationPlugin()
    monkeypatch.setattr(plugin, "_user_plugin_dir", lambda: write_parent(tmp_path))
    channel, responses = bind(plugin)

    payload = (await call(channel, responses, "paths"))["payload"]

    assert payload["user_plugin_dir"] == str(tmp_path / "application_plugins")
    assert payload["search_paths"]
    assert any(path.endswith("extensions") for path in payload["search_paths"])
    assert payload["default_extension_dir"] == "jiuwenswarm/extensions"
    assert payload["max_plugins"] == plugin_module.MAX_PLUGINS
    assert isinstance(payload["running_version"], str)


@pytest.mark.asyncio
async def test_rules_handler_returns_full_catalog() -> None:
    channel, responses = bind(PluginDoctorApplicationPlugin())

    payload = (await call(channel, responses, "rules"))["payload"]

    assert payload["count"] == len(doctor_engine.RULES)
    assert payload["rules"][0]["rule_id"] == "PD001"
    assert all(rule["evidence"] for rule in payload["rules"])


# --------------------------------------------------------------------------
# plugin.doctor.report
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_report_handler_scans_and_caches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = write_parent(tmp_path)
    write_plugin(root)
    plugin = PluginDoctorApplicationPlugin()
    monkeypatch.setattr(plugin, "_user_plugin_dir", lambda: root)
    channel, responses = bind(plugin)
    responses.clear()

    first = (await call(channel, responses, "report"))["payload"]
    second = (await call(channel, responses, "report"))["payload"]
    forced = (await call(channel, responses, "report", {"refresh": True}))["payload"]

    # 默认搜索目录 = 用户插件目录 + 配置目录 + 包内 extensions（会一并扫到内置插件）
    plugin_ids = {item["plugin_id"] for item in first["plugins"]}
    assert "good-plugin" in plugin_ids
    assert first["scanned"] == len(first["plugins"])
    assert first["cached"] is False
    assert first["max_plugins"] == plugin_module.MAX_PLUGINS
    assert second["cached"] is True
    assert forced["cached"] is False
    assert first["errors"] == 0


@pytest.mark.asyncio
async def test_report_handler_accepts_explicit_search_paths(tmp_path: Path) -> None:
    target = write_plugin(tmp_path)
    plugin = PluginDoctorApplicationPlugin()
    channel, responses = bind(plugin)

    from_string = (
        await call(channel, responses, "report", {"search_paths": str(target.parent)})
    )["payload"]
    from_list = (
        await call(channel, responses, "report", {"search_paths": [str(target.parent)]})
    )["payload"]

    assert from_string["scanned"] == 1
    assert from_list["scanned"] == 1
    assert plugin._cached is None


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [123, "", [], ["  "], {"a": 1}])
async def test_report_handler_rejects_bad_search_paths(bad: Any) -> None:
    channel, responses = bind(PluginDoctorApplicationPlugin())

    response = await call(channel, responses, "report", {"search_paths": bad})

    assert response["ok"] is False
    assert response["code"] == plugin_module.BAD_REQUEST


@pytest.mark.asyncio
async def test_report_handler_trims_to_max_plugins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = write_parent(tmp_path)
    write_plugin(root, "one-plugin", HEALTHY_MANIFEST.replace("good-plugin", "one-plugin"))
    write_plugin(root, "two-plugin", HEALTHY_MANIFEST.replace("good-plugin", "two-plugin"))
    plugin = PluginDoctorApplicationPlugin()
    monkeypatch.setattr(plugin, "_user_plugin_dir", lambda: root)
    monkeypatch.setattr(plugin_module, "MAX_PLUGINS", 1)
    channel, responses = bind(plugin)

    payload = (await call(channel, responses, "report"))["payload"]

    assert payload["scanned"] == 1
    assert payload["max_plugins"] == 1


# --------------------------------------------------------------------------
# plugin.doctor.inspect
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [None, 123, "", "   ", []])
async def test_inspect_handler_requires_path(bad: Any) -> None:
    channel, responses = bind(PluginDoctorApplicationPlugin())

    response = await call(channel, responses, "inspect", {"path": bad})

    assert response["ok"] is False
    assert response["code"] == plugin_module.BAD_REQUEST


@pytest.mark.asyncio
async def test_inspect_handler_rejects_path_outside_search_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = write_parent(tmp_path)
    outside = write_plugin(tmp_path / "outside", "outside-plugin")
    plugin = PluginDoctorApplicationPlugin()
    monkeypatch.setattr(plugin, "_user_plugin_dir", lambda: root)
    channel, responses = bind(plugin)

    response = await call(channel, responses, "inspect", {"path": str(outside)})

    assert response["ok"] is False
    assert response["code"] == plugin_module.PATH_NOT_ALLOWED


@pytest.mark.asyncio
async def test_inspect_handler_accepts_relative_path_inside_user_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = write_parent(tmp_path)
    write_plugin(root, "broken-plugin", BROKEN_MANIFEST)
    plugin = PluginDoctorApplicationPlugin()
    monkeypatch.setattr(plugin, "_user_plugin_dir", lambda: root)
    channel, responses = bind(plugin)

    payload = (await call(channel, responses, "inspect", {"path": "broken-plugin"}))["payload"]

    rule_ids = {finding["rule_id"] for finding in payload["findings"]}
    assert "PD003" in rule_ids
    assert payload["status"] == "broken"
    assert payload["score"] < 100


@pytest.mark.asyncio
async def test_inspect_handler_reports_clean_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = write_parent(tmp_path)
    write_plugin(root)
    plugin = PluginDoctorApplicationPlugin()
    monkeypatch.setattr(plugin, "_user_plugin_dir", lambda: root)
    channel, responses = bind(plugin)

    payload = (
        await call(channel, responses, "inspect", {"path": str(root / "good-plugin")})
    )["payload"]

    assert payload["status"] == "ok"
    assert payload["score"] == 100
    assert payload["findings"] == []


# --------------------------------------------------------------------------
# plugin.doctor.export
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_export_json_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = write_parent(tmp_path)
    write_plugin(root)
    plugin = PluginDoctorApplicationPlugin()
    monkeypatch.setattr(plugin, "_user_plugin_dir", lambda: root)
    channel, responses = bind(plugin)

    payload = (await call(channel, responses, "export", {"format": "json"}))["payload"]

    assert payload["filename"] == "plugin-doctor-report.json"
    assert payload["mime"] == "application/json"
    document = json.loads(payload["content"])
    assert document["scanned"] == len(document["plugins"])
    assert "good-plugin" in {item["plugin_id"] for item in document["plugins"]}


@pytest.mark.asyncio
async def test_export_markdown_uses_cached_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = write_parent(tmp_path)
    write_plugin(root)
    plugin = PluginDoctorApplicationPlugin()
    monkeypatch.setattr(plugin, "_user_plugin_dir", lambda: root)
    channel, responses = bind(plugin)
    await call(channel, responses, "report")

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("export 不应该重新扫描")

    monkeypatch.setattr(plugin, "_scan", _boom)
    response = await call(channel, responses, "export", {"format": "markdown"})

    assert response["ok"] is True
    assert response["payload"]["filename"] == "plugin-doctor-report.md"
    assert response["payload"]["content"].startswith("# 应用插件体检报告")


@pytest.mark.asyncio
async def test_export_writes_back_the_scan_to_the_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没有先调 report 就直接导出时，这次扫描要写回缓存，连续导出不再重复扫盘。"""

    root = write_parent(tmp_path)
    write_plugin(root)
    plugin = PluginDoctorApplicationPlugin()
    monkeypatch.setattr(plugin, "_user_plugin_dir", lambda: root)
    channel, responses = bind(plugin)
    scans: list[Any] = []
    original = plugin._scan

    def counting(*args: Any, **kwargs: Any) -> Any:
        scans.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(plugin, "_scan", counting)

    first = await call(channel, responses, "export", {"format": "json"})
    second = await call(channel, responses, "export", {"format": "json"})

    assert first["ok"] is True and second["ok"] is True
    assert len(scans) == 1
    assert plugin._cached is not None


@pytest.mark.asyncio
async def test_report_passes_the_real_yaml_availability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PD005 不能是死代码：插件要把真实的 PyYAML 可用性传给引擎。"""

    root = write_parent(tmp_path)
    write_plugin(root)
    plugin = PluginDoctorApplicationPlugin()
    monkeypatch.setattr(plugin, "_user_plugin_dir", lambda: root)
    monkeypatch.setattr(plugin_module, "_YAML_AVAILABLE", False)
    channel, responses = bind(plugin)

    payload = (await call(channel, responses, "report"))["payload"]

    ids = {
        finding["rule_id"]
        for item in payload["plugins"]
        for finding in item["findings"]
    }
    assert "PD005" in ids


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["pdf", "", "JSON5"])
async def test_export_handler_rejects_unknown_format(bad: str) -> None:
    channel, responses = bind(PluginDoctorApplicationPlugin())

    response = await call(channel, responses, "export", {"format": bad})

    assert response["ok"] is False
    assert response["code"] == plugin_module.BAD_REQUEST


# --------------------------------------------------------------------------
# 搜索目录与工具函数
# --------------------------------------------------------------------------


def test_resolve_search_paths_drops_missing_and_dedupes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = write_parent(tmp_path)
    real = tmp_path / "extra"
    real.mkdir()
    plugin = PluginDoctorApplicationPlugin()
    monkeypatch.setattr(plugin, "_user_plugin_dir", lambda: root)
    plugin._config = {"extensions": {"extension_dirs": f"{real};{tmp_path / 'ghost'};{real}"}}

    paths = plugin._resolve_search_paths(None)

    assert paths[0] == root
    assert real.resolve() in paths
    assert len(paths) == len(set(paths))
    assert tmp_path / "ghost" not in paths


def test_config_extension_dirs_only_accepts_strings() -> None:
    plugin = PluginDoctorApplicationPlugin()

    plugin._config = {}
    assert plugin._config_extension_dirs() == []
    plugin._config = {"extensions": "nope"}
    assert plugin._config_extension_dirs() == []
    plugin._config = {"extensions": {"extension_dirs": ["a", "b"]}}
    assert plugin._config_extension_dirs() == []
    plugin._config = {"extensions": {"extension_dirs": "a; b ;;"}}
    assert plugin._config_extension_dirs() == ["a", "b"]


def test_candidate_paths_adds_bundled_directory_for_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = PluginDoctorApplicationPlugin()

    candidates = plugin._candidate_paths("jiuwenswarm/extensions")

    assert len(candidates) == 2
    assert candidates[1] == Path(plugin_module.__file__).resolve().parents[1]
    assert plugin._candidate_paths("some/relative/dir") == [Path("some/relative/dir")]
    assert plugin._candidate_paths(str(Path.cwd())) == [Path.cwd()]


@pytest.mark.parametrize(
    "value,expected",
    [
        ("a;b", ["a", "b"]),
        (["a", " b "], ["a", "b"]),
        (("a",), ["a"]),
    ],
)
def test_as_dir_list_variants(value: Any, expected: list[str]) -> None:
    assert PluginDoctorApplicationPlugin._as_dir_list(value) == expected


@pytest.mark.parametrize("bad", [1, None, [], {}, ""])
def test_as_dir_list_rejects_invalid(bad: Any) -> None:
    with pytest.raises(ValueError):
        PluginDoctorApplicationPlugin._as_dir_list(bad)


def test_is_within(tmp_path: Path) -> None:
    inside = tmp_path / "a" / "b"
    outside = tmp_path / "c"

    assert PluginDoctorApplicationPlugin._is_within(inside, [tmp_path / "a"]) is True
    assert PluginDoctorApplicationPlugin._is_within(outside, [tmp_path / "a"]) is False


def test_dependency_installed_check() -> None:
    # 这里比对的是 pip 发行包名（清单 dependencies 的键）
    assert PluginDoctorApplicationPlugin._dependency_installed("pytest") is True
    assert PluginDoctorApplicationPlugin._dependency_installed("PyYAML") is True
    assert PluginDoctorApplicationPlugin._dependency_installed("no-such-pkg-xyz") is False
    assert PluginDoctorApplicationPlugin._dependency_installed("not a valid name!!") is False
    # 导入名不等于发行包名：清单里写 yaml 会被判成缺依赖，这正是 PD021 想提醒的
    assert PluginDoctorApplicationPlugin._dependency_installed("yaml") is False


def test_running_version_helper_returns_string() -> None:
    version = plugin_module.running_jiuwenswarm_version()

    assert isinstance(version, str)
    if version:
        assert version[0].isdigit()
