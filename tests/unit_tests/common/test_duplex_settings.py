from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from jiuwenswarm.common import config
from jiuwenswarm.common.config_panel import config_set_handlers as handlers
from jiuwenswarm.common.config_panel.models_handlers import ConfigPanelBadRequest
from jiuwenswarm.common.duplex_router import env_secret


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("preferred_language: en\nother: keep\nduplex_router: {}\n")
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", path)
    monkeypatch.setattr(handlers, "get_config_raw", lambda: yaml.safe_load(path.read_text()))
    monkeypatch.setattr(handlers, "get_config", lambda: yaml.safe_load(path.read_text()))
    monkeypatch.setattr(handlers, "_get_crypto_provider", lambda: None)
    monkeypatch.setattr(handlers, "persist_env_updates", lambda updates: None)
    monkeypatch.setenv("DUPLEX_ROUTER_API_KEY", "")
    return path


@pytest.mark.asyncio
async def test_settings_round_trip_through_current_config_pipeline(settings_file):
    values = {"enabled": "true", "mode": "active", "backend": "jev", "model_name": "jev-1.13.0",
              "timeout_seconds": "4", "interrupt_threshold": "0.95",
              "api_base": "https://api.typesafe.ai/v1", "endpoint_path": "decisions"}
    result = handlers.apply_config_payload({"duplex_router_" + key: value for key, value in values.items()})
    raw = yaml.safe_load(settings_file.read_text())
    assert raw["other"] == "keep"
    assert raw["duplex_router"]["enabled"] is True
    assert raw["duplex_router"]["jev"]["interrupt_threshold"] == 0.95
    assert raw["duplex_router"]["timeout_seconds"] == 4
    assert len(result.yaml_updated) == len(values)
    channel = SimpleNamespace(send_response=AsyncMock())
    await handlers.config_get_handler(channel, None, "r1", {}, "s1")
    payload = channel.send_response.await_args.kwargs["payload"]
    for key, value in values.items():
        expected = "4.0" if key == "timeout_seconds" else value
        assert payload["duplex_router_" + key] == expected
    assert payload["duplex_router_backends"]["jev"] == {
        "api_base": values["api_base"], "interrupt_threshold": "0.95"}
    assert payload["duplex_router_backends"]["mindshub"]["api_base"] == "https://api.mindshub.ai/v1"


def test_supervisor_can_be_disabled_without_clearing_settings(settings_file):
    handlers.apply_config_payload({"duplex_router_mode": "active", "duplex_router_enabled": "false"})
    raw = yaml.safe_load(settings_file.read_text())["duplex_router"]
    assert raw["enabled"] is False
    assert raw["mode"] == "active"


@pytest.mark.parametrize("field,value", [("mode", "shadow"), ("backend", "unknown"),
    ("timeout_seconds", "nan"), ("timeout_seconds", "0"), ("interrupt_threshold", "0.5"),
    ("interrupt_threshold", "1.1"), ("api_base", "file:///tmp/secret"), ("endpoint_path", "../other")])
def test_invalid_settings_are_rejected_without_writing(settings_file, field, value):
    before = settings_file.read_bytes()
    with pytest.raises(ConfigPanelBadRequest):
        handlers.apply_config_payload({"duplex_router_" + field: value})
    assert settings_file.read_bytes() == before


def test_jev_key_uses_existing_environment_key_flow(settings_file):
    result = handlers.apply_config_payload({"duplex_router_api_key": "unit-test-key"})
    assert result.env_updates == {"DUPLEX_ROUTER_API_KEY": "unit-test-key"}
    assert env_secret("DUPLEX_ROUTER_API_KEY") == "unit-test-key"
    assert "unit-test-key" not in settings_file.read_text()


@pytest.mark.parametrize("backend,base", [("mindshub", "https://api.mindshub.ai/v1"),
                                        ("clef", "https://api.cloudflare.com/client/v4")])
def test_external_endpoint_updates_only_selected_backend(settings_file, backend, base):
    handlers.apply_config_payload({"duplex_router_backend": backend, "duplex_router_api_base": base})
    raw = yaml.safe_load(settings_file.read_text())["duplex_router"]
    assert raw[backend]["api_base"] == base
    assert "jev" not in raw


def test_clef_account_and_model_use_runtime_settings(settings_file, monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "")
    result = handlers.apply_config_payload({"duplex_router_account_id": "test-account",
                                           "duplex_router_model": "clef-flash"})
    assert result.env_updates["CLOUDFLARE_ACCOUNT_ID"] == "test-account"
    assert yaml.safe_load(settings_file.read_text())["duplex_router"]["clef"]["model"] == "clef-flash"
