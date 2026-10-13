# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""A2A duplex supervisor settings and their TeamAgentSpec wiring."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from jiuwenswarm.agents.harness.team.config_loader import load_team_spec_dict
from jiuwenswarm.common import config
from jiuwenswarm.common.config_panel import config_set_handlers as handlers
from jiuwenswarm.common.config_panel.duplex_router_settings import jev_defaults
from jiuwenswarm.common.config_panel.models_handlers import ConfigPanelBadRequest

KEY_ENV = "DUPLEX_ROUTER_API_KEY"
ENABLED = {
    "duplex_router_enabled": "true",
    "duplex_router_mode": "active",
    "duplex_router_format": "anthropic",
    "duplex_router_api_base": "https://relay.example/v1",
    "duplex_router_model": "claude-opus",
    "duplex_router_timeout_seconds": "3.5",
    "duplex_router_api_key": "unit-test-key",
}


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("preferred_language: en\nother: keep\n")
    env_writes = []
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", path)
    monkeypatch.setattr(handlers, "get_config_raw", lambda: yaml.safe_load(path.read_text()))
    monkeypatch.setattr(handlers, "get_config", lambda: yaml.safe_load(path.read_text()))
    monkeypatch.setattr(handlers, "_get_crypto_provider", lambda: None)
    monkeypatch.setattr(handlers, "persist_env_updates", env_writes.append)
    monkeypatch.setenv(KEY_ENV, "")
    return SimpleNamespace(path=path, env_writes=env_writes)


async def _config_get():
    channel = SimpleNamespace(send_response=AsyncMock())
    await handlers.config_get_handler(channel, None, "r1", {}, "s1")
    return channel.send_response.await_args.kwargs["payload"]


@pytest.mark.asyncio
async def test_settings_round_trip_keeps_key_in_env(settings_file):
    result = handlers.apply_config_payload(dict(ENABLED))

    raw = yaml.safe_load(settings_file.path.read_text())
    assert raw["other"] == "keep"
    assert raw["duplex_router"] == {
        "enabled": True, "mode": "active", "format": "anthropic", "api_base": "https://relay.example/v1",
        "model": "claude-opus", "timeout_seconds": 3.5, "api_key_env": KEY_ENV,
    }
    assert "unit-test-key" not in settings_file.path.read_text()
    assert result.env_updates == {KEY_ENV: "unit-test-key"}
    payload = await _config_get()
    assert payload["duplex_router_api_key"] == ""  # write-only
    assert payload["duplex_router_api_key_configured"] == "true"
    for key in ("enabled", "mode", "format", "api_base", "model"):
        assert payload[f"duplex_router_{key}"] == ENABLED[f"duplex_router_{key}"]
    assert payload["duplex_router_timeout_seconds"] == "3.5"


def test_invalid_value_is_rejected_without_writing(settings_file):
    before = settings_file.path.read_bytes()
    with pytest.raises(ConfigPanelBadRequest):
        handlers.apply_config_payload({**ENABLED, "duplex_router_api_base": "file:///tmp/secret"})
    assert settings_file.path.read_bytes() == before
    assert settings_file.env_writes == []


def test_clear_disables_and_clears_secret(settings_file):
    handlers.apply_config_payload(dict(ENABLED))
    result = handlers.apply_config_payload({
        "duplex_router_enabled": "false", "duplex_router_mode": "off",
        "duplex_router_api_base": "", "duplex_router_model": "", "duplex_router_api_key": "",
    })
    assert result.env_updates == {KEY_ENV: ""}
    raw = yaml.safe_load(settings_file.path.read_text())["duplex_router"]
    assert raw["enabled"] is False and raw["mode"] == "off" and raw["api_base"] == "" and raw["model"] == ""


def test_jev_endpoint_and_key_follow_url_host(settings_file, monkeypatch):
    jev = {"duplex_router_enabled": "true", "duplex_router_mode": "active", "duplex_router_format": "jev",
           "duplex_router_api_base": "https://openrouter.ai/api/alpha"}
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    with pytest.raises(ConfigPanelBadRequest, match="OPENROUTER_API_KEY"):
        handlers.apply_config_payload(dict(jev))
    # A typed key overrides the shared browser Jev key.
    handlers.apply_config_payload({**jev, "duplex_router_api_key": "own"})
    assert yaml.safe_load(settings_file.path.read_text())["duplex_router"]["api_key_env"] == KEY_ENV
    monkeypatch.setenv("OPENROUTER_API_KEY", "set")
    handlers.apply_config_payload({**jev, "duplex_router_api_key": ""})
    assert yaml.safe_load(settings_file.path.read_text())["duplex_router"]["api_key_env"] == ""
    # Another JEV host needs its own model and key.
    with pytest.raises(ConfigPanelBadRequest, match="model, api_key"):
        handlers.apply_config_payload({**jev, "duplex_router_api_base": "https://mcl.example/v1"})
    # A relay key does not follow a switch to another format / host.
    monkeypatch.setenv("TYPESAFE_API_KEY", "set")
    handlers.apply_config_payload(dict(ENABLED))
    handlers.apply_config_payload({**jev, "duplex_router_api_base": "https://api.typesafe.ai/v1"})
    assert yaml.safe_load(settings_file.path.read_text())["duplex_router"]["api_key_env"] == ""
    assert [jev_defaults(base)["endpoint"] for base in ("", "https://openrouter.ai/x", "https://x.edu")] == [
        "/systemone", "/decisions", "/decisions",
    ]


def test_wiring_adds_duplex_only_when_sdk_declares_it(monkeypatch):
    from openjiuwen.agent_teams.schema import blueprint

    section = {"enabled": True, "mode": "active", "format": "jev", "api_base": "", "endpoint": "",
               "model": "jev-1.13.0", "api_key_env": "", "timeout_seconds": 2.0}
    base = {"duplex_router": section, "modes": {"team": {"t": {"agents": {"leader": {}, "teammate": {}}}}}}

    monkeypatch.setattr(blueprint, "TeamAgentSpec", SimpleNamespace(model_fields={}))
    assert "duplex" not in load_team_spec_dict(config_base=base)

    monkeypatch.setattr(blueprint, "TeamAgentSpec", SimpleNamespace(model_fields={"duplex": object()}))
    assert load_team_spec_dict(config_base=base)["duplex"] == section
    # Off (or a hand-edited bare `mode: off`): the key is omitted.
    off = {**base, "duplex_router": {**section, "mode": False, "format": "bad"}}
    assert "duplex" not in load_team_spec_dict(config_base=off)
