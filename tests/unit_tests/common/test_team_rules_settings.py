# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Group team rules settings and their TeamAgentSpec wiring."""

from types import SimpleNamespace

import pytest
import yaml

from jiuwenswarm.agents.harness.team import config_loader
from jiuwenswarm.agents.harness.team.config_loader import load_team_spec_dict
from jiuwenswarm.common import config
from jiuwenswarm.common.config_panel import config_set_handlers as handlers
from jiuwenswarm.common.config_panel.models_handlers import ConfigPanelBadRequest
from jiuwenswarm.common.team_rules import team_rules_panel_view, resolve_team_rules_spec
from jiuwenswarm.server.control import config_service

TEAM = {"modes": {"team": {"t": {"agents": {"leader": {}, "teammate": {}}}}}}
MAIN_MODEL_RAW = {
    "models": {"defaults": [{"model_id": "mdl_default_primary", "model_client_config": {
        "api_base": "${API_BASE}", "api_key": "${API_KEY}", "model_name": "${MODEL_NAME}",
        "client_provider": "${MODEL_PROVIDER}",
    }}]},
}


def _main_model(provider="OpenAI", api_base="https://relay.example/v1/", model="deepseek-v4-flash"):
    return {"model_id": "mdl_default_primary", "model_client_config": {
        "api_base": api_base, "api_key": "resolved-secret", "model_name": model,
        "client_provider": provider,
    }}


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(
        "preferred_language: en\nother: keep\nteam_rules:\n  enabled: true\n  format: openai\n"
    )
    env_writes = []
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", path)
    monkeypatch.setattr(handlers, "get_config_raw", lambda: yaml.safe_load(path.read_text()))
    monkeypatch.setattr(handlers, "get_config", lambda: yaml.safe_load(path.read_text()))
    monkeypatch.setattr(handlers, "_get_crypto_provider", lambda: None)
    monkeypatch.setattr(handlers, "persist_env_updates", env_writes.append)
    env_file = tmp_path / ".env"
    env_file.write_text("")
    monkeypatch.setattr(handlers, "get_env_file", lambda: env_file)
    for name in ("TYPESAFE_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.setenv(name, "")
    return SimpleNamespace(
        path=path, env_file=env_file, env_writes=env_writes,
        read=lambda: yaml.safe_load(path.read_text()),
    )


def test_save_writes_only_the_mode(settings_file, monkeypatch):
    handlers.apply_config_payload({"team_rules_mode": "llm"})

    raw = settings_file.read()
    assert raw["other"] == "keep"
    # Keys from older versions are dropped; nothing goes to .env.
    assert raw["team_rules"] == {"mode": "llm"}
    assert settings_file.env_writes == []
    monkeypatch.setattr(config_service, "_read_raw_config", settings_file.read)
    assert config_service.get_panel()["team_rules_mode"] == "llm"


def test_invalid_mode_writes_nothing(settings_file):
    before = settings_file.path.read_bytes()
    for bad in ("grpc", "active", ""):
        with pytest.raises(ConfigPanelBadRequest, match="team_rules.mode"):
            handlers.apply_config_payload({"team_rules_mode": bad})
    assert settings_file.path.read_bytes() == before


def test_save_goes_through_config_write_lock(settings_file, monkeypatch):
    locked = []
    real_update_config = config.update_config

    def spy(mutator, **kwargs):
        locked.append(True)
        return real_update_config(mutator, **kwargs)

    monkeypatch.setattr(config, "update_config", spy)
    config.update_team_rules_in_config({"mode": "off"})
    assert locked == [True]
    # The round-trip writer leaves ``off`` bare; a YAML 1.1 reader sees False, read as off.
    assert team_rules_panel_view(settings_file.read()) == {"team_rules_mode": "off"}
    assert settings_file.read()["other"] == "keep"


def test_panel_reads_unknown_or_missing_mode_as_off():
    assert team_rules_panel_view({})["team_rules_mode"] == "off"
    assert team_rules_panel_view({"team_rules": {"mode": "grpc"}})["team_rules_mode"] == "off"
    # YAML 1.1 reads a bare ``off`` as False.
    assert team_rules_panel_view({"team_rules": {"mode": False}})["team_rules_mode"] == "off"
    off = {"team_rules": {"mode": "off"}}
    assert resolve_team_rules_spec(off, main_model=_main_model()) is None


def test_jev_save_needs_the_shared_jev_key(settings_file, monkeypatch):
    missing = "Jev API key is required in Models > Jev settings"
    with pytest.raises(ConfigPanelBadRequest, match=missing):
        handlers.apply_config_payload({"team_rules_mode": "jev"})
    monkeypatch.setenv("TYPESAFE_API_KEY", "browser-key")
    handlers.apply_config_payload({"team_rules_mode": "jev"})
    raw = settings_file.read()
    assert raw["team_rules"] == {"mode": "jev"}
    assert resolve_team_rules_spec(raw) == {
        "enabled": True, "mode": "active", "format": "jev",
        "api_base": "https://api.typesafe.ai/v1",
        "endpoint": "/systemone", "model": "jev-1.13.0", "api_key_env": "TYPESAFE_API_KEY",
    }


def test_jev_key_saved_to_env_by_another_process_counts(settings_file):
    # The panel reads the saved .env; saving must accept the same key even if os.environ is stale.
    settings_file.env_file.write_text('TYPESAFE_API_KEY="browser-key"\n')
    handlers.apply_config_payload({"team_rules_mode": "jev"})
    assert settings_file.read()["team_rules"]["mode"] == "jev"


def test_jev_follows_the_shared_decision_provider(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "router-key")
    base = {"browser": {"decision": {"provider": "openrouter"}}, "team_rules": {"mode": "jev"}}
    spec = resolve_team_rules_spec(base)
    assert (spec["api_base"], spec["endpoint"], spec["model"], spec["api_key_env"]) == (
        "https://openrouter.ai/api/alpha", "/decisions", "typesafe/jev-1.13", "OPENROUTER_API_KEY",
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    with pytest.raises(ValueError, match="Jev API key is required"):
        resolve_team_rules_spec(base)


def test_llm_uses_the_main_model():
    cfg = {"team_rules": {"mode": "llm"}}
    spec = resolve_team_rules_spec(cfg, main_model=_main_model(), main_model_key_env="API_KEY")
    assert spec == {
        "enabled": True, "mode": "active", "format": "openai",
        "api_base": "https://relay.example/v1",
        "model": "deepseek-v4-flash", "api_key_env": "API_KEY",
    }
    anthropic = resolve_team_rules_spec(
        cfg, main_model=_main_model(provider="Anthropic"), main_model_key_env="API_KEY",
    )
    assert anthropic["format"] == "anthropic"
    # OpenAI-compatible aliases use the OpenAI format.
    alias = resolve_team_rules_spec(
        cfg, main_model=_main_model(provider="DeepSeek"), main_model_key_env="API_KEY",
    )
    assert alias["format"] == "openai"
    with pytest.raises(ValueError, match="mode llm"):
        resolve_team_rules_spec(cfg, main_model=_main_model(), main_model_key_env="")


def testmain_model_key_env_follows_the_config_reference(monkeypatch):
    monkeypatch.setattr(config_loader, "get_config_raw", lambda: MAIN_MODEL_RAW)
    assert config_loader.main_model_key_env(_main_model()) == "API_KEY"
    assert not config_loader.main_model_key_env({"model_id": "other"})
    stored = {"models": {"defaults": [{"model_id": "mdl_default_primary",
                                       "model_client_config": {"api_key": "enc:abc"}}]}}
    monkeypatch.setattr(config_loader, "get_config_raw", lambda: stored)
    assert not config_loader.main_model_key_env(_main_model())
    monkeypatch.setattr(config_loader, "get_config_raw", lambda: {})
    assert config_loader.main_model_key_env(_main_model()) == "API_KEY"


def test_loader_adds_team_rules_only_when_sdk_declares_it(monkeypatch):
    from openjiuwen.agent_teams.schema import blueprint

    monkeypatch.setattr(config_loader, "get_config_raw", lambda: MAIN_MODEL_RAW)
    monkeypatch.setattr(config_loader, "get_default_models", lambda _cfg=None: [_main_model()])
    base = {**TEAM, "team_rules": {"mode": "llm"}}
    if blueprint.TeamAgentSpec.model_fields.get("team_rules") is None:
        assert "team_rules" not in load_team_spec_dict(config_base=base)
        fake = SimpleNamespace(model_fields={"team_rules": SimpleNamespace(annotation=dict)})
        monkeypatch.setattr(blueprint, "TeamAgentSpec", fake)
    team_rules = load_team_spec_dict(config_base=base)["team_rules"]
    got = (team_rules["mode"], team_rules["format"], team_rules["model"], team_rules["api_key_env"])
    assert got == (
        "active", "openai", "deepseek-v4-flash", "API_KEY",
    )
    off = {**base, "team_rules": {"mode": "off"}}
    assert "team_rules" not in load_team_spec_dict(config_base=off)
    # Jev without a key: the team starts with the original delivery.
    jev = {**base, "team_rules": {"mode": "jev"}}
    assert "team_rules" not in load_team_spec_dict(config_base=jev)
