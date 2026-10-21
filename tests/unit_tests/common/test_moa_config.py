# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Experimental collaboration settings, atomic saves and write-only credentials."""
from copy import deepcopy
import sys
from types import ModuleType

import pytest
from jiuwenswarm.common.moa_config import (
    build_moa_config_from_frontend, flatten_moa_for_config_panel,
    redact_moa_credentials, save_moa_config_from_frontend,
)


def test_defaults_ignore_archival_configuration():
    flat = flatten_moa_for_config_panel({"moa": {"jev": {"api_key": "old-secret"}, "verifier": {"type": "jev", "interval": 1}}})
    assert flat == {
        "multi_model_collaboration_enabled": "false", "moa_jev_enabled": "false",
        "moa_verifier_interval": "3", "moa_jev_api_base": "https://api.typesafe.ai",
        "moa_jev_model_name": "jev-latest", "moa_jev_api_key": "",
        "moa_jev_api_key_configured": "false",
    }


@pytest.mark.parametrize("interval", [1, "1", 3, "100000"])
def test_positive_interval_independent_of_model_groups(interval):
    result = build_moa_config_from_frontend({"moa_verifier_interval": interval}, {})
    assert result["multi_model_collaboration"]["verifier_interval"] == int(interval)


@pytest.mark.parametrize("interval", ["", None, 0, "0", -1, "-3", 1.5, "3.0", "1e3", True])
def test_invalid_intervals_rejected(interval):
    with pytest.raises(ValueError, match="positive integer"):
        build_moa_config_from_frontend({"moa_verifier_interval": interval}, {})


@pytest.mark.parametrize("field", ["multi_model_collaboration_enabled", "moa_jev_enabled", "moa_jev_api_key_clear"])
@pytest.mark.parametrize("value", [1, 0, "yes", {}, None])
def test_boolean_fields_require_explicit_bool(field, value):
    with pytest.raises(ValueError, match="boolean"):
        build_moa_config_from_frontend({field: value}, {})


@pytest.mark.parametrize("field,value", [
    ("moa_jev_api_key", ""), ("moa_jev_api_base", ""), ("moa_jev_model_name", ""),
    ("moa_jev_api_base", "ftp://example.com"),
    ("moa_jev_api_base", "https://user:secret@example.com"),
    ("moa_jev_api_base", "https://"),
])
def test_jev_enable_requires_complete_connection(field, value):
    with pytest.raises(ValueError, match="JEV"):
        build_moa_config_from_frontend({"moa_jev_enabled": True, "moa_jev_api_key": "new-secret", field: value}, {})


@pytest.mark.parametrize("secret_params", [
    {}, {"moa_jev_api_key": None}, {"moa_jev_api_key": ""}, {"moa_jev_api_key": "  "},
])
def test_blank_secret_preserves_new_ciphertext_and_never_reads_old(secret_params):
    raw = {"experimental": {"multi_model_collaboration": {"jev": {"api_key": "ciphertext"}}},
           "moa": {"jev": {"api_key": "old-secret"}}}
    params = {"moa_jev_enabled": True, **secret_params}
    saved = build_moa_config_from_frontend(params, raw)
    assert saved["multi_model_collaboration"]["jev"]["api_key"] == "ciphertext"
    flat = flatten_moa_for_config_panel({"experimental": saved})
    assert flat["moa_jev_api_key"] == ""
    assert flat["moa_jev_api_key_configured"] == "true"
    assert "ciphertext" not in str(flat)
    with pytest.raises(ValueError, match="JEV api_key"):
        build_moa_config_from_frontend({"moa_jev_enabled": True}, {"moa": raw["moa"]})


def test_clear_secret_disables_jev_atomically_even_with_blank_form():
    raw = {"experimental": {"multi_model_collaboration_enabled": True, "multi_model_collaboration": {
        "jev_enabled": True, "verifier_interval": 1, "jev": {"api_key": "ciphertext"},
    }}}
    result = build_moa_config_from_frontend({"moa_jev_api_key_clear": True, "moa_jev_enabled": True}, raw)
    assert result["multi_model_collaboration_enabled"] is True
    assert result["multi_model_collaboration"]["jev_enabled"] is False
    assert result["multi_model_collaboration"]["jev"]["api_key"] == ""
    assert result["multi_model_collaboration"]["verifier_interval"] == 1


def test_unresolved_environment_secret_is_not_configured():
    raw = {"experimental": {"multi_model_collaboration": {"jev": {"api_key": "${JEV_API_KEY}"}}}}
    def resolve(value):
        return "" if value == "${JEV_API_KEY}" else value
    assert flatten_moa_for_config_panel(raw, resolve_value=resolve)["moa_jev_api_key_configured"] == "false"
    with pytest.raises(ValueError, match="JEV api_key"):
        build_moa_config_from_frontend({"moa_jev_enabled": True}, raw, resolve_value=resolve)


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


def test_save_one_transaction_preserves_legacy_and_encrypts_only_new_secret(monkeypatch):
    raw = {"moa": {"old": "preserve"}, "experimental": {"team_organization_ui_enabled": True}, "unrelated": 42}
    calls = _install_config_boundary(monkeypatch, raw)
    encrypted = []
    def encrypt(secret):
        encrypted.append(secret)
        return "ciphertext"
    save_moa_config_from_frontend({"multi_model_collaboration_enabled": True, "moa_jev_enabled": True,
                                  "moa_jev_api_key": " new-secret "}, encrypt_secret=encrypt)
    assert calls == [True] and encrypted == ["new-secret"]
    assert raw["moa"] == {"old": "preserve"} and raw["unrelated"] == 42
    assert raw["experimental"]["team_organization_ui_enabled"] is True
    save_moa_config_from_frontend({"multi_model_collaboration_enabled": False, "moa_jev_api_key": ""}, encrypt_secret=encrypt)
    assert encrypted == ["new-secret"]
    assert raw["experimental"]["multi_model_collaboration"]["jev_enabled"] is True
    save_moa_config_from_frontend({"moa_jev_api_key_clear": True}, encrypt_secret=encrypt)
    assert encrypted == ["new-secret"]
    assert raw["experimental"]["multi_model_collaboration"]["jev"]["api_key"] == ""
    assert raw["experimental"]["multi_model_collaboration"]["jev_enabled"] is False


def test_encryption_failure_never_exposes_secret_or_updates_config(monkeypatch):
    raw = {"unrelated": 42}
    _install_config_boundary(monkeypatch, raw)
    def broken_encrypt(secret):
        raise ValueError(f"provider could not encrypt {secret}")
    with pytest.raises(RuntimeError, match="failed to encrypt JEV credential") as caught:
        save_moa_config_from_frontend({"moa_jev_api_key": "secret-value"}, encrypt_secret=broken_encrypt)
    assert "secret-value" not in str(caught.value) and caught.value.__suppress_context__
    assert raw == {"unrelated": 42}


@pytest.mark.parametrize("params,expected_secret", [
    ({}, "ciphertext"),
    ({"moa_jev_api_key": None}, "ciphertext"),
    ({"moa_jev_api_key": ""}, "ciphertext"),
    ({"moa_jev_api_key": "  "}, "ciphertext"),
    ({"moa_jev_api_key": "new-secret", "moa_jev_api_key_clear": True}, ""),
])
def test_save_retained_or_cleared_secret_never_encrypts(
    monkeypatch: pytest.MonkeyPatch, params: dict, expected_secret: str,
) -> None:
    raw = {"experimental": {"multi_model_collaboration": {"jev": {"api_key": "ciphertext"}}}}
    calls = _install_config_boundary(monkeypatch, raw)

    def unexpected_encrypt(secret: str) -> str:
        pytest.fail("Retaining or clearing a credential must not encrypt it")

    save_moa_config_from_frontend(params, encrypt_secret=unexpected_encrypt)

    assert calls == [True]
    assert raw["experimental"]["multi_model_collaboration"]["jev"]["api_key"] == expected_secret


def test_save_new_secret_without_encryption_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = {}
    _install_config_boundary(monkeypatch, raw)

    save_moa_config_from_frontend({"moa_jev_api_key": " new-secret "})

    assert raw["experimental"]["multi_model_collaboration"]["jev"]["api_key"] == "new-secret"


@pytest.mark.parametrize("secret", [False, 1, [], {}])
def test_invalid_secret_type_does_not_update_config(monkeypatch: pytest.MonkeyPatch, secret: object) -> None:
    raw = {"unrelated": 42}
    _install_config_boundary(monkeypatch, raw)

    with pytest.raises(ValueError, match="moa_jev_api_key must be a string"):
        save_moa_config_from_frontend({"moa_jev_api_key": secret})

    assert raw == {"unrelated": 42}


def test_transport_redaction_is_recursive_and_keeps_wire_original():
    wire = {"params": {"config": {"moa_jev_api_key": "secret-value", "moa_verifier_interval": "3"}}}
    masked = redact_moa_credentials([wire])
    assert "secret-value" not in str(masked)
    assert masked[0]["params"]["config"]["moa_verifier_interval"] == "3"
    assert wire["params"]["config"]["moa_jev_api_key"] == "secret-value"


def test_disabling_collaboration_preserves_invalid_jev_without_requiring_credentials():
    raw = {"experimental": {"multi_model_collaboration_enabled": True, "multi_model_collaboration": {
        "jev_enabled": True, "jev": {"api_base": "", "api_key": "${MISSING_KEY}"}, "verifier_interval": 3,
    }}}
    saved = build_moa_config_from_frontend({"multi_model_collaboration_enabled": False}, raw)
    assert saved["multi_model_collaboration_enabled"] is False
    assert saved["multi_model_collaboration"]["jev_enabled"] is True
    assert saved["multi_model_collaboration"]["jev"]["api_key"] == "${MISSING_KEY}"
    with pytest.raises(ValueError, match="JEV"):
        build_moa_config_from_frontend({"multi_model_collaboration_enabled": True}, {"experimental": saved})
