# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Group team rules settings and their TeamAgentSpec wiring."""

from types import SimpleNamespace

import pytest
import yaml

from jiuwenswarm.agents.harness.team.config_loader import load_team_spec_dict
from jiuwenswarm.common import config
from jiuwenswarm.common.config_panel import config_set_handlers as handlers
from jiuwenswarm.common.config_panel.models_handlers import ConfigPanelBadRequest
from jiuwenswarm.common.team_rules import team_rules_panel_view, resolve_team_rules_spec
from jiuwenswarm.server.control import config_service

KEY_ENV = "TEAM_RULES_API_KEY"
ENABLED = {
    "team_rules_enabled": "true",
    "team_rules_format": "anthropic",
    "team_rules_api_base": "https://relay.example/v1",
    "team_rules_model": "claude-opus",
    "team_rules_api_key": "unit-test-key",
}
TEAM = {"modes": {"team": {"t": {"agents": {"leader": {}, "teammate": {}}}}}}


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
    env_file = tmp_path / ".env"
    env_file.write_text("")
    monkeypatch.setattr(handlers, "get_env_file", lambda: env_file)
    for name in (KEY_ENV, "TYPESAFE_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.setenv(name, "")
    return SimpleNamespace(
        path=path, env_file=env_file, env_writes=env_writes, read=lambda: yaml.safe_load(path.read_text()),
    )


def test_save_keeps_key_in_env_and_only_its_name_in_yaml(settings_file, monkeypatch):
    result = handlers.apply_config_payload(dict(ENABLED))

    raw = settings_file.read()
    assert raw["other"] == "keep"
    assert raw["team_rules"] == {
        "enabled": True, "format": "anthropic", "api_base": "https://relay.example/v1",
        "model": "claude-opus", "api_key_env": KEY_ENV,
    }
    assert "unit-test-key" not in settings_file.path.read_text()
    assert result.env_updates == {KEY_ENV: "unit-test-key"}
    monkeypatch.setattr(config_service, "_read_raw_config", settings_file.read)
    monkeypatch.setattr(config_service, "_browser_credential_environment", lambda: {KEY_ENV: "unit-test-key"})
    panel = config_service.get_panel()
    for key in ("enabled", "format", "api_base", "model", "api_key"):
        assert panel[f"team_rules_{key}"] == ENABLED[f"team_rules_{key}"]


def test_invalid_or_incomplete_save_writes_nothing(settings_file):
    before = settings_file.path.read_bytes()
    for bad in (
        {**ENABLED, "team_rules_api_base": "file:///tmp/secret"},
        {**ENABLED, "team_rules_api_base": "https://user:pw@relay.example/v1"},
        {**ENABLED, "team_rules_api_base": "https://relay.example/v1#frag"},
        {**ENABLED, "team_rules_format": "grpc"},
        {**ENABLED, "team_rules_model": ""},
        {**ENABLED, "team_rules_api_key": ""},
    ):
        with pytest.raises(ConfigPanelBadRequest):
            handlers.apply_config_payload(bad)
    assert settings_file.path.read_bytes() == before
    assert settings_file.env_writes == []


def test_save_goes_through_config_write_lock(settings_file, monkeypatch):
    locked = []
    real_update_config = config.update_config

    def spy(mutator, **kwargs):
        locked.append(True)
        return real_update_config(mutator, **kwargs)

    monkeypatch.setattr(config, "update_config", spy)
    config.update_team_rules_in_config({"enabled": False})
    assert locked == [True]
    assert settings_file.read()["team_rules"] == {"enabled": False}
    assert settings_file.read()["other"] == "keep"


def test_turning_off_needs_nothing_else(settings_file):
    handlers.apply_config_payload(dict(ENABLED))
    handlers.apply_config_payload({"team_rules_enabled": "false", "team_rules_api_key": ""})
    raw = settings_file.read()["team_rules"]
    assert raw["enabled"] is False and raw["api_key_env"] == ""
    assert resolve_team_rules_spec(settings_file.read()) is None


def test_jev_without_own_key_reuses_browser_jev(settings_file, monkeypatch):
    jev = {"team_rules_enabled": "true", "team_rules_format": "jev",
           "team_rules_api_base": "", "team_rules_model": "", "team_rules_api_key": ""}
    with pytest.raises(ConfigPanelBadRequest, match="api_key"):
        handlers.apply_config_payload(dict(jev))
    monkeypatch.setenv("TYPESAFE_API_KEY", "browser-key")
    handlers.apply_config_payload(dict(jev))
    raw = settings_file.read()
    assert raw["team_rules"]["api_key_env"] == ""
    assert resolve_team_rules_spec(raw) == {
        "enabled": True, "mode": "active", "format": "jev", "api_base": "https://api.typesafe.ai/v1",
        "endpoint": "/systemone", "model": "jev-1.13.0", "api_key_env": "TYPESAFE_API_KEY",
    }
    view = team_rules_panel_view(raw)
    assert view["team_rules_jev_key_configured"] == "true"
    assert view["team_rules_jev_model"] == "jev-1.13.0"



def test_jev_key_saved_to_env_by_another_process_counts(settings_file):
    # The panel reads the saved .env; saving must accept the same key even if os.environ is stale.
    settings_file.env_file.write_text('TYPESAFE_API_KEY="browser-key"\n')
    handlers.apply_config_payload({"team_rules_enabled": "true", "team_rules_format": "jev",
                                   "team_rules_api_base": "", "team_rules_model": "",
                                   "team_rules_api_key": ""})
    assert settings_file.read()["team_rules"]["format"] == "jev"

def test_jev_follows_browser_provider_and_own_key_wins():
    base = {"browser": {"decision": {"provider": "openrouter"}},
            "team_rules": {"enabled": True, "format": "jev"}}
    spec = resolve_team_rules_spec(base)
    assert (spec["api_base"], spec["endpoint"], spec["model"], spec["api_key_env"]) == (
        "https://openrouter.ai/api/alpha", "/decisions", "typesafe/jev-1.13", "OPENROUTER_API_KEY",
    )
    own = {**base, "team_rules": {"enabled": True, "format": "jev",
                                  "api_base": "https://mcl.example/v1", "model": "m", "api_key_env": KEY_ENV}}
    spec = resolve_team_rules_spec(own)
    assert spec["api_key_env"] == KEY_ENV and "endpoint" not in spec


def test_loader_adds_team_rules_only_when_sdk_declares_it(monkeypatch):
    from openjiuwen.agent_teams.schema import blueprint

    base = {**TEAM, "team_rules": {"enabled": True, "format": "openai",
                                   "api_base": "https://x.example/v1", "model": "m", "api_key_env": KEY_ENV}}
    if blueprint.TeamAgentSpec.model_fields.get("team_rules") is None:
        assert "team_rules" not in load_team_spec_dict(config_base=base)
        fake = SimpleNamespace(model_fields={"team_rules": SimpleNamespace(annotation=dict)})
        monkeypatch.setattr(blueprint, "TeamAgentSpec", fake)
    assert load_team_spec_dict(config_base=base)["team_rules"]["mode"] == "active"
    off = {**base, "team_rules": {**base["team_rules"], "enabled": False}}
    assert "team_rules" not in load_team_spec_dict(config_base=off)
