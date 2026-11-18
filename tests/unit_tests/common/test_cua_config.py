# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Desktop preferences round-trip independently of backend availability."""

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import yaml

from jiuwenswarm.common import config, cua_config
from jiuwenswarm.common.config_panel import config_set_handlers as handlers
from jiuwenswarm.server.control import config_service


@pytest.mark.parametrize("entry, expected", [
    (None, False), ({}, False), ({"enabled": None}, False),
    ({"enabled": False}, False), ({"enabled": True}, True), ({"enabled": "true"}, False),
])
def test_desktop_preference_resolves_only_explicit_booleans(entry, expected):
    assert cua_config.is_cua_agent_enabled(entry) is expected


@pytest.fixture
def panel_config(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("react:\n  subagents:\n    cua_agent:\n      max_iterations: 7\n", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", path)
    monkeypatch.setattr(handlers, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(handlers, "_get_crypto_provider", lambda: None)
    monkeypatch.setattr(cua_config, "cua_repl_unavailable_reason", lambda: "native_build_failed")
    monkeypatch.setattr(cua_config, "is_legacy_cua_available", lambda: False)
    monkeypatch.setattr(config_service, "get_config_file", lambda: path)
    monkeypatch.setattr(config_service, "get_env_file", lambda: tmp_path / ".env")
    original = handlers.provider
    a2ui = SimpleNamespace(get_a2ui_config_payload=lambda raw: {}, get_default_a2ui_config_payload=lambda: {})
    monkeypatch.setattr(handlers, "provider", lambda domain: a2ui if domain == "a2ui" else original(domain))
    return path


@pytest.mark.asyncio
@pytest.mark.parametrize("value, expected", [(True, True), (False, False), (None, None), ("true", True), ("false", False)])
async def test_desktop_preference_round_trips_through_both_config_readers(panel_config, value, expected):
    channel = SimpleNamespace(send_response=AsyncMock())
    await handlers.config_save_all_handler(channel, None, "save", {"config": {"cua_agent_enabled": value}}, None)
    assert channel.send_response.call_args.kwargs["ok"] is True
    saved = yaml.safe_load(panel_config.read_text(encoding="utf-8"))["react"]["subagents"]["cua_agent"]
    assert saved == {"enabled": expected, "max_iterations": 7}
    await handlers.config_get_handler(channel, None, "get", {}, None)
    for payload in (channel.send_response.call_args.kwargs["payload"], config_service.get_panel()):
        assert payload["cua_agent_enabled"] == ("true" if expected else "false")
        assert payload["cua_backend_available"] == "false"
        assert payload["cua_backend_unavailable_reason"] == "native_build_failed"


@pytest.fixture
def permission_guide_sdk(tmp_path, monkeypatch):
    bundle = tmp_path / "runtime" / "Jiuwen CUA.app"
    driver = Mock(return_value={"command": str(bundle / "Contents/MacOS/computer-use-driver")})
    launch = Mock()
    monkeypatch.setitem(sys.modules, "computer_use.paths", SimpleNamespace(native_driver=driver))
    monkeypatch.setitem(sys.modules, "computer_use.native.macos_app", SimpleNamespace(open_permission_guide=launch))
    return bundle, driver, launch


@pytest.mark.asyncio
@pytest.mark.parametrize("save_all", [True, False], ids=["config.save_all", "config.set"])
async def test_enabling_desktop_control_opens_the_macos_guide_once(
    panel_config, permission_guide_sdk, monkeypatch, save_all,
):
    bundle, driver, launch = permission_guide_sdk
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(cua_config, "cua_repl_unavailable_reason", lambda: "")
    channel = SimpleNamespace(send_response=AsyncMock())
    saved_during_launch = []

    def capture_saved_preference(_bundle, *, timeout):
        saved = yaml.safe_load(panel_config.read_text(encoding="utf-8"))
        saved_during_launch.append(saved["react"]["subagents"]["cua_agent"]["enabled"])

    launch.side_effect = capture_saved_preference
    for value in ("true", True, False, None):
        params = {"cua_agent_enabled": value}
        if save_all:
            await handlers.config_save_all_handler(channel, None, "save", {"config": params}, None)
        else:
            await handlers.config_set_handler(channel, None, "save", params, None)
        assert channel.send_response.call_args.kwargs["ok"] is True
    driver.assert_called_once_with()
    launch.assert_called_once()
    assert launch.call_args.args == (bundle,)
    assert saved_during_launch == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize(("system", "reason"), [("win32", ""), ("linux", ""), ("darwin", "backend_missing")])
async def test_enabling_desktop_control_skips_the_guide_without_a_macos_backend(
    panel_config, permission_guide_sdk, monkeypatch, system, reason,
):
    _, driver, launch = permission_guide_sdk
    monkeypatch.setattr(sys, "platform", system)
    monkeypatch.setattr(cua_config, "cua_repl_unavailable_reason", lambda: reason)
    channel = SimpleNamespace(send_response=AsyncMock())
    await handlers.config_save_all_handler(
        channel, None, "save", {"config": {"cua_agent_enabled": True}}, None,
    )
    assert channel.send_response.call_args.kwargs["ok"] is True
    driver.assert_not_called()
    launch.assert_not_called()


@pytest.mark.asyncio
async def test_macos_guide_launch_failure_keeps_the_saved_preference(
    panel_config, permission_guide_sdk, monkeypatch, caplog,
):
    _, _, launch = permission_guide_sdk
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(cua_config, "cua_repl_unavailable_reason", lambda: "")
    launch.side_effect = RuntimeError("launch failed")
    channel = SimpleNamespace(send_response=AsyncMock())
    await handlers.config_save_all_handler(
        channel, None, "save", {"config": {"cua_agent_enabled": True}}, None,
    )
    assert channel.send_response.call_args.kwargs["ok"] is True
    saved = yaml.safe_load(panel_config.read_text(encoding="utf-8"))
    assert saved["react"]["subagents"]["cua_agent"]["enabled"] is True
    assert "launch failed" in caplog.text


@pytest.mark.parametrize("value", ["yes", 1, {}, []])
def test_invalid_desktop_preference_does_not_modify_config(panel_config, value):
    before = panel_config.read_bytes()
    with pytest.raises(handlers.ConfigPanelBadRequest):
        handlers.apply_config_payload({"cua_agent_enabled": value})
    assert panel_config.read_bytes() == before


@pytest.mark.parametrize("system", ["darwin", "win32"])
def test_frozen_backend_uses_external_resource_layout(tmp_path, monkeypatch, system):
    monkeypatch.delenv("JIUWENSWARM_RUNTIME_PLATFORM", raising=False)
    monkeypatch.setattr(cua_config.sys, "platform", system)
    monkeypatch.setattr(cua_config.sys, "frozen", True, raising=False)
    monkeypatch.setattr(cua_config.sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setattr(cua_config.platform, "mac_ver", lambda: ("14.0", (), ""))
    monkeypatch.setattr(cua_config.importlib.util, "find_spec", lambda name: object())
    assert not cua_config.is_cua_repl_available()
    root = tmp_path / "computer_use/_resources/os_adapter"
    files = (["macos/.build/Jiuwen CUA.app/Contents/MacOS/computer-use-driver"] if system == "darwin"
             else ["windows/.build/" + name for name in (
                 "computer-use-driver.exe", "computer-use-capture.exe", "Interop.NativeUIA.dll",
             )])
    for relative in files:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        path.chmod(0o755)
    assert cua_config.is_cua_repl_available()
    monkeypatch.setattr(cua_config.importlib.util, "find_spec", lambda name: None if name == "computer_use_mcp" else object())
    assert not cua_config.is_cua_repl_available()
    monkeypatch.setattr(cua_config.importlib.util, "find_spec", lambda name: object())
    path.unlink()
    assert not cua_config.is_cua_repl_available()


@pytest.mark.parametrize("result", [None, {}, {"command": None}, AttributeError("SDK probe failed")])
def test_frozen_macos_sdk_probe_failure_keeps_settings_available(tmp_path, monkeypatch, result):
    monkeypatch.setattr(cua_config, "sys", SimpleNamespace(platform="darwin", frozen=True, _MEIPASS=str(tmp_path)))
    monkeypatch.setattr(cua_config, "_is_harmony_desktop", lambda: False)
    monkeypatch.setattr(cua_config.platform, "mac_ver", lambda: ("14.0", (), ""))
    monkeypatch.setattr(cua_config.importlib.util, "find_spec", lambda name: object())

    def native_driver(*, platform, root):
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setitem(sys.modules, "computer_use.paths", SimpleNamespace(native_driver=native_driver))

    assert cua_config.cua_backend_settings() == {
        "cua_backend_available": "false",
        "cua_backend_unavailable_reason": "backend_missing",
    }


@pytest.mark.parametrize(("version", "installed", "reason", "expected"), [
    ("14.0", False, None, "backend_missing"),
    ("14.0", True, None, ""),
    ("14.0", True, "native_build_failed", "native_build_failed"),
    ("14.0", True, RuntimeError("SDK probe failed"), "backend_missing"),
    ("13.6", True, None, "unsupported_platform"),
])
def test_source_macos_reports_driver_availability_without_building(monkeypatch, version, installed, reason, expected):
    monkeypatch.setattr(cua_config.sys, "platform", "darwin")
    monkeypatch.setattr(cua_config.sys, "frozen", False, raising=False)
    monkeypatch.setattr(cua_config.platform, "mac_ver", lambda: (version, (), ""))
    monkeypatch.setattr(cua_config.importlib.util, "find_spec", lambda name: object() if installed else None)

    def check_backend():
        if isinstance(reason, Exception):
            raise reason
        return reason

    paths = SimpleNamespace(installed_backend_unavailable_reason=check_backend)
    monkeypatch.setitem(sys.modules, "computer_use", SimpleNamespace(paths=paths))
    assert cua_config.cua_repl_unavailable_reason() == expected
    assert cua_config.is_cua_backend_available() is (not expected)


@pytest.mark.parametrize(("system", "legacy_available"), [
    ("darwin", False), ("win32", False), ("linux", True),
])
def test_legacy_fallback_after_repl_build_failure_is_linux_only(monkeypatch, system, legacy_available):
    monkeypatch.setattr(cua_config, "sys", SimpleNamespace(platform=system))
    monkeypatch.setattr(cua_config, "_is_harmony_desktop", lambda: False)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(cua_config.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(cua_config, "cua_repl_unavailable_reason", lambda: "native_build_failed")

    assert not cua_config.is_cua_repl_available()
    assert cua_config.is_legacy_cua_available() is legacy_available
    assert cua_config.is_cua_backend_available() is legacy_available
    assert cua_config.cua_backend_settings() == {
        "cua_backend_available": "true" if legacy_available else "false",
        "cua_backend_unavailable_reason": "" if legacy_available else "native_build_failed",
    }


def test_harmony_has_no_desktop_backend_even_with_sdk_and_legacy_dependencies(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_RUNTIME_PLATFORM", " Harmony ")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(cua_config.sys, "platform", "linux")
    monkeypatch.setattr(cua_config.sys, "frozen", False, raising=False)
    monkeypatch.setattr(cua_config.importlib.util, "find_spec", lambda name: object())

    assert not cua_config.is_cua_repl_available()
    assert not cua_config.is_legacy_cua_available()
    assert not cua_config.is_cua_backend_available()
