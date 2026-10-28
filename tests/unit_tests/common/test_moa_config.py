# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Collaboration switches use the shared Jev connection without duplicating credentials."""
from copy import deepcopy
import sys
from types import ModuleType

import pytest

from jiuwenswarm.common.browser_settings import resolve_jev_connection
from jiuwenswarm.common.moa_config import (
    build_moa_config_from_frontend, flatten_moa_for_config_panel,
    redact_moa_credentials, save_moa_config_from_frontend,
)


@pytest.fixture(autouse=True)
def isolated_keys(monkeypatch):
    for name in ("TYPESAFE_API_KEY", "OPENROUTER_API_KEY", "JEV_CUSTOM_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def test_defaults_ignore_archival_configuration():
    flat = flatten_moa_for_config_panel({
        "moa": {"jev": {"api_key": "old-secret"}},
        "experimental": {"multi_model_collaboration": {"jev": {"api_key": "obsolete-secret"}}},
    })
    assert flat == {
        "multi_model_collaboration_enabled": "false", "moa_jev_enabled": "false",
        "moa_verifier_interval": "3",
    }


@pytest.mark.parametrize("interval", [1, "1", 3, "100000"])
def test_positive_interval_independent_of_model_groups(interval):
    result = build_moa_config_from_frontend({"moa_verifier_interval": interval}, {})
    assert result["multi_model_collaboration"]["verifier_interval"] == int(interval)
    assert "jev" not in result["multi_model_collaboration"]


@pytest.mark.parametrize("interval", ["", None, 0, "0", -1, "-3", 1.5, "3.0", "1e3", True])
def test_invalid_intervals_rejected(interval):
    with pytest.raises(ValueError, match="positive integer"):
        build_moa_config_from_frontend({"moa_verifier_interval": interval}, {})


@pytest.mark.parametrize("field", ["multi_model_collaboration_enabled", "moa_jev_enabled"])
@pytest.mark.parametrize("value", [1, 0, "yes", {}, None])
def test_boolean_fields_require_explicit_bool(field, value):
    with pytest.raises(ValueError, match="boolean"):
        build_moa_config_from_frontend({field: value}, {})


@pytest.mark.parametrize("field", [
    "moa_jev_api_key", "moa_jev_api_base", "moa_jev_model_name", "moa_jev_api_key_clear",
])
def test_obsolete_connection_writes_are_explicitly_rejected(field):
    with pytest.raises(ValueError, match="Models > Jev"):
        build_moa_config_from_frontend({field: "obsolete-value"}, {})


@pytest.mark.parametrize("provider,base,key_env,endpoint", [
    ("typesafe", "https://api.typesafe.ai/v1", "TYPESAFE_API_KEY", "/systemone"),
    ("openrouter", "https://openrouter.ai/api/alpha", "OPENROUTER_API_KEY", "/decisions"),
    ("custom", "https://jev.example.test/v1", "JEV_CUSTOM_API_KEY", "/systemone"),
])
def test_shared_connection_works_while_browser_policy_is_off(monkeypatch, provider, base, key_env, endpoint):
    monkeypatch.setenv(key_env, "shared-key")
    raw = {"browser": {"decision": {"mode": "llm", "provider": provider, "api_base": base}}}
    connection = resolve_jev_connection(raw)
    assert connection["api_key"] == "shared-key"
    assert connection["api_base"] == base
    assert connection["endpoint_path"] == endpoint
    saved = build_moa_config_from_frontend({"moa_jev_enabled": True}, raw)
    assert saved["multi_model_collaboration"]["jev_enabled"] is True
    assert "jev" not in saved["multi_model_collaboration"]
    assert raw["browser"]["decision"]["mode"] == "llm"


@pytest.mark.parametrize("field", ["api_base", "model", "api_key_env"])
@pytest.mark.parametrize("value", [None, "", "  ", 123])
def test_shared_connection_rejects_invalid_required_values(field, value):
    raw = {"browser": {"decision": {"provider": "typesafe", field: value}}}
    with pytest.raises(ValueError, match=f"Jev {field} is required"):
        resolve_jev_connection(raw)


def test_shared_connection_normalizes_validated_values_without_mutating_config(monkeypatch):
    monkeypatch.setenv("CUSTOM_JEV_API_KEY", " shared-key ")
    raw = {"browser": {"decision": {
        "provider": "custom", "api_base": " https://jev.example.test/v1/ ",
        "model": " jev-custom ", "api_key_env": " CUSTOM_JEV_API_KEY ",
    }}}
    before = deepcopy(raw)
    assert resolve_jev_connection(raw) == {
        "api_base": "https://jev.example.test/v1", "model_name": "jev-custom",
        "api_key": "shared-key", "endpoint_path": "/systemone",
    }
    assert raw == before


@pytest.mark.parametrize("base", ["", "ftp://example.com", "https://user:secret@example.com", "https://", "https://host/?key=secret"])
def test_invalid_shared_endpoint_cannot_enable_jev(monkeypatch, base):
    monkeypatch.setenv("JEV_CUSTOM_API_KEY", "shared-key")
    raw = {"browser": {"decision": {"provider": "custom", "api_base": base}}}
    with pytest.raises(ValueError, match="Jev"):
        build_moa_config_from_frontend({"moa_jev_enabled": True}, raw)


def test_legacy_secret_does_not_override_missing_shared_key():
    raw = {"experimental": {"multi_model_collaboration": {"jev": {"api_key": "obsolete"}}}}
    with pytest.raises(ValueError, match="Models > Jev"):
        build_moa_config_from_frontend({"moa_jev_enabled": True}, raw)


def _install_config_boundary(monkeypatch, current):
    module = ModuleType("jiuwenswarm.common.config")
    calls = []

    def update_config(mutate):
        calls.append(True)
        candidate = mutate(deepcopy(current))
        current.clear()
        current.update(candidate)

    module.update_config = update_config
    module.resolve_env_vars = lambda value: value
    monkeypatch.setitem(sys.modules, "jiuwenswarm.common.config", module)
    return calls


def test_save_only_controls_preserves_shared_connection_and_archived_values(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "shared-key")
    raw = {
        "browser": {"decision": {"mode": "llm", "provider": "typesafe"}},
        "experimental": {"multi_model_collaboration": {"jev": {"api_key": "archived"}}},
        "unrelated": 42,
    }
    before = deepcopy(raw)
    calls = _install_config_boundary(monkeypatch, raw)
    save_moa_config_from_frontend({
        "multi_model_collaboration_enabled": True, "moa_jev_enabled": True, "moa_verifier_interval": 5,
    })
    assert calls == [True]
    assert raw["browser"] == before["browser"]
    assert raw["unrelated"] == 42
    assert raw["experimental"]["multi_model_collaboration"]["jev"] == {"api_key": "archived"}
    assert "shared-key" not in str(raw)
    save_moa_config_from_frontend({"moa_jev_enabled": False})
    assert raw["experimental"]["multi_model_collaboration"]["jev_enabled"] is False


def test_obsolete_write_does_not_mutate_config(monkeypatch):
    raw = {"unrelated": 42}
    _install_config_boundary(monkeypatch, raw)
    with pytest.raises(ValueError, match="Models > Jev"):
        save_moa_config_from_frontend({"moa_jev_api_key": "secret-value"})
    assert raw == {"unrelated": 42}


def test_transport_redaction_is_recursive_and_keeps_wire_original():
    wire = {"params": {"config": {"moa_jev_api_key": "secret-value", "moa_verifier_interval": "3"}}}
    masked = redact_moa_credentials([wire])
    assert "secret-value" not in str(masked)
    assert masked[0]["params"]["config"]["moa_verifier_interval"] == "3"
    assert wire["params"]["config"]["moa_jev_api_key"] == "secret-value"


def test_disabling_collaboration_preserves_invalid_jev_without_requiring_credentials():
    raw = {"experimental": {"multi_model_collaboration_enabled": True, "multi_model_collaboration": {
        "jev_enabled": True, "jev": {"api_key": "obsolete"}, "verifier_interval": 3,
    }}}
    saved = build_moa_config_from_frontend({"multi_model_collaboration_enabled": False}, raw)
    assert saved["multi_model_collaboration_enabled"] is False
    assert saved["multi_model_collaboration"]["jev_enabled"] is True
    assert saved["multi_model_collaboration"]["jev"]["api_key"] == "obsolete"
    with pytest.raises(ValueError, match="Jev"):
        build_moa_config_from_frontend({"multi_model_collaboration_enabled": True}, {"experimental": saved})
