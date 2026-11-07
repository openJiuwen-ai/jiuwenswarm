# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Desktop preferences round-trip independently of backend availability."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

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
    monkeypatch.setattr(handlers, "is_cua_backend_available", lambda: False)
    monkeypatch.setattr(config_service, "is_cua_backend_available", lambda: False)
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
    assert not cua_config.is_cua_backend_available()
    root = tmp_path / "computer_use/_resources/os_adapter"
    files = (["macos/.build/ComputerUseDriver.app/Contents/MacOS/computer-use-driver"] if system == "darwin"
             else ["windows/.build/" + name for name in (
                 "computer-use-driver.exe", "computer-use-capture.exe", "Interop.NativeUIA.dll",
             )])
    for relative in files:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        path.chmod(0o755)
    assert cua_config.is_cua_backend_available()
    path.unlink()
    assert not cua_config.is_cua_backend_available()


def test_source_macos_hides_control_without_importing_the_external_sdk(monkeypatch):
    monkeypatch.setattr(cua_config.sys, "platform", "darwin")
    monkeypatch.setattr(cua_config.sys, "frozen", False, raising=False)
    monkeypatch.setattr(cua_config.importlib.util, "find_spec", lambda name: pytest.fail("unexpected import check"))
    assert not cua_config.is_cua_backend_available()
