# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Configuration cache preserves live inputs and independent caller state."""

import json
from types import SimpleNamespace

import pytest
import yaml

from jiuwenswarm.common import config


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("channels: {}\nmodels: {}\nvalue: ${STARTUP_VALUE:-fallback}\n", encoding="utf-8")
    monkeypatch.setattr(config, "get_config_file", lambda: path)
    monkeypatch.setattr(config, "get_crypto_provider", lambda: None)
    monkeypatch.delenv("STARTUP_VALUE", raising=False)
    config._YAML_PARSE_CACHE.clear()
    config._RESOLVED_CONFIG_CACHE.clear()
    yield path
    config._YAML_PARSE_CACHE.clear()
    config._RESOLVED_CONFIG_CACHE.clear()


def test_resolved_cache_observes_environment_changes(config_path, monkeypatch):
    assert config.get_config()["value"] == "fallback"
    monkeypatch.setenv("STARTUP_VALUE", "updated")
    assert config.get_config()["value"] == "updated"
    monkeypatch.setenv("STARTUP_VALUE", "")
    assert config.get_config()["value"] == "fallback"
    monkeypatch.delenv("STARTUP_VALUE")
    assert config.get_config()["value"] == "fallback"


def test_unchanged_snapshot_normalizes_once_and_is_not_shared(config_path, monkeypatch):
    original = config._normalize_config
    calls = []

    def normalize(value):
        calls.append(value)
        original(value)

    monkeypatch.setattr(config, "_normalize_config", normalize)
    first = config.get_config()
    first["channels"]["web"]["send_file_allowed"] = False
    assert config.get_config()["channels"]["web"]["send_file_allowed"] is True
    assert len(calls) == 1


def test_snapshot_invalidates_after_external_file_edit(config_path):
    assert config.get_config()["value"] == "fallback"
    config_path.write_text("channels: {}\nmodels: {}\nvalue: external-change\n", encoding="utf-8")
    assert config.get_config()["value"] == "external-change"


def test_replacing_reader_does_not_reuse_previous_snapshot(config_path, monkeypatch):
    assert config.get_config()["value"] == "fallback"
    with monkeypatch.context() as scoped:
        scoped.setattr(config, "_read_with_retry", lambda _path: {"sandbox": {"type": "jiuwenbox"}})
        assert config.get_config()["sandbox"]["type"] == "jiuwenbox"
        assert "value" not in config.get_config()
    assert config.get_config()["value"] == "fallback"


def test_cached_mcp_placeholders_and_mutable_headers_are_independent(config_path, monkeypatch):
    config_path.write_text(
        "channels: {}\nmodels: {}\nmcp:\n  servers:\n  - name: local\n"
        "    transport: sse\n    url: http://127.0.0.1\n"
        "    headers:\n      Authorization: 'Bearer ${MCP_TOKEN}'\n", encoding="utf-8")
    monkeypatch.setenv("MCP_TOKEN", "process-token")
    first = config.get_config()
    first["mcp"]["servers"][0]["headers"]["Authorization"] = "changed"
    assert config.get_config()["mcp"]["servers"][0]["headers"] == {
        "Authorization": "Bearer ${MCP_TOKEN}"}


def test_cached_decryption_observes_provider_replacement(config_path, monkeypatch):
    config_path.write_text("channels: {}\nmodels: {}\nkey: ${API_KEY}\n", encoding="utf-8")
    monkeypatch.setenv("API_KEY", "encrypted")

    class Provider:
        def __init__(self, result):
            self.result = result

        def decrypt(self, _value):
            return self.result

    first, second = Provider("first"), Provider("second")
    monkeypatch.setattr(config, "get_crypto_provider", lambda: first)
    assert config.get_config()["key"] == "first"
    monkeypatch.setattr(config, "get_crypto_provider", lambda: second)
    assert config.get_config()["key"] == "second"


def test_decryption_observes_extension_registration_through_stable_bridge(config_path, monkeypatch):
    from agent_gateway.extensions import base_crypto
    from agent_gateway.extensions.registry import ExtensionRegistry

    config_path.write_text("channels: {}\nmodels: {}\nkey: ${API_KEY}\n", encoding="utf-8")
    monkeypatch.setenv("API_KEY", "synthetic-encrypted")
    monkeypatch.setattr(ExtensionRegistry, "_instance", None)
    monkeypatch.setattr(base_crypto, "_default_provider", None)
    monkeypatch.setattr(config, "get_crypto_provider", base_crypto.get_crypto_provider)
    registry = ExtensionRegistry.create_instance(callback_framework=object(), config={}, logger=None)
    bridge = base_crypto.get_crypto_provider()
    assert config.get_config()["key"] == "synthetic-encrypted"

    for value in ("first", "second"):
        provider = SimpleNamespace(decrypt=lambda _ciphertext, result=value: result)
        registry.register_crypto_utility(SimpleNamespace(get_crypto=lambda current=provider: current))
        assert base_crypto.get_crypto_provider() is bridge
        assert config.get_config()["key"] == value


@pytest.mark.parametrize("fallback", [False, True])
def test_safe_loader_and_python_fallback_preserve_yaml_types(config_path, monkeypatch, fallback):
    if fallback:
        monkeypatch.delattr(yaml, "CSafeLoader", raising=False)
    config_path.write_text("channels: {}\nmodels: {}\nflag: true\nitems: [1, '2']\n", encoding="utf-8")
    result = config.get_config()
    assert result["flag"] is True and result["items"] == [1, "2"]
    config_path.write_text("!!python/object/apply:builtins.str [1]\n", encoding="utf-8")
    with pytest.raises(yaml.YAMLError):
        config._read_with_retry(config_path, max_attempts=1)


def test_header_normalization_refreshes_referenced_environment(config_path, monkeypatch):
    config_path.write_text(
        "channels: {}\nmodels:\n  custom:\n    model_client_config:\n"
        "      custom_headers: '${STARTUP_HEADERS}'\n", encoding="utf-8")
    for value in ("one", "two"):
        monkeypatch.setenv("STARTUP_HEADERS", json.dumps({"X-Test": value}))
        assert config.get_config()["models"]["custom"]["model_client_config"]["custom_headers"] == {
            "X-Test": value}


@pytest.mark.parametrize("initial_status", ["pending", "failed"])
def test_snapshot_observes_exec_credential_completion(config_path, monkeypatch, initial_status):
    from jiuwenswarm.common.credentials import registry, resolver

    reference = "exec:synthetic-command-must-not-run"
    config_path.write_text(json.dumps({
        "models": {"defaults": [{"model_client_config": {"api_key": reference}}]},
    }), encoding="utf-8")
    monkeypatch.setattr(registry, "_load_patterns", lambda: [
        ["models", "*", "model_client_config", "api_key"],
    ])
    monkeypatch.setattr(resolver, "_RESOLVED_SECRETS", {})

    def unexpected_command(*_args, **_kwargs):
        pytest.fail("configuration reads must not execute credential commands")

    monkeypatch.setattr(resolver, "_exec_secret_command", unexpected_command)
    resolver._release_claim(reference, "", initial_status)
    assert config.get_config()["models"]["defaults"][0]["model_client_config"]["api_key"] == ""
    stamp = config_path.stat()

    for value in ("synthetic-ready", "synthetic-refreshed"):
        resolver._release_claim(reference, value, "resolved")
        snapshot = config.get_config()
        assert snapshot["models"]["defaults"][0]["model_client_config"]["api_key"] == value
        snapshot["models"]["defaults"][0]["model_client_config"]["api_key"] = "caller-only"
        assert config.get_config()["models"]["defaults"][0]["model_client_config"]["api_key"] == value
    assert (stamp.st_mtime_ns, stamp.st_size) == (config_path.stat().st_mtime_ns, config_path.stat().st_size)
