"""Unit tests for SecretStore (L1–L4)."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from jiuwenswarm.common.secrets import SecretStore
from jiuwenswarm.common.secrets.registry import SecretRegistry
from jiuwenswarm.common.secrets.transform import SecretTransform
from jiuwenswarm.common.secrets.persistence import (
    DefaultFileStorageBackend,
    EnvMediumAdapter,
    FileMediumAdapter,
    PersistenceGateway,
)


@pytest.fixture
def secret_home(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    workspace_dir = tmp_path / "workspace"
    config_dir.mkdir()
    workspace_dir.mkdir()
    monkeypatch.setenv("JIUWENSWARM_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(tmp_path))
    return config_dir, workspace_dir


def _build_store(config_dir: Path, workspace_dir: Path, registry_yaml: str | None = None) -> SecretStore:
    if registry_yaml is not None:
        (config_dir / "secret_registry.yaml").write_text(registry_yaml, encoding="utf-8")
    registry = SecretRegistry(config_dir=config_dir, workspace_dir=workspace_dir, bundled_path=Path("__missing__"))
    transform = SecretTransform()
    gateway = PersistenceGateway(
        env_adapter=EnvMediumAdapter(config_dir / ".env"),
        file_adapter=FileMediumAdapter(config_dir=config_dir, workspace_dir=workspace_dir),
        default_backend=DefaultFileStorageBackend(config_dir / "secrets_store.json"),
    )
    return SecretStore(registry=registry, transform=transform, gateway=gateway)


class TestDefaultStorage:
    def test_set_get_delete_roundtrip(self, secret_home):
        config_dir, workspace_dir = secret_home
        store = _build_store(config_dir, workspace_dir)
        SecretStore.reset_for_tests(store)
        store.set("my_feature.api_key", "sk-plain")
        assert store.get("my_feature.api_key") == "sk-plain"
        store.delete("my_feature.api_key")
        assert store.get("my_feature.api_key") == ""

    def test_get_missing_returns_empty(self, secret_home):
        config_dir, workspace_dir = secret_home
        store = _build_store(config_dir, workspace_dir)
        assert store.get("nonexistent.key") == ""


class TestEnvMedium:
    def test_registry_env_binding(self, secret_home):
        config_dir, workspace_dir = secret_home
        yaml_text = """
llm.api_key:
  medium: env
  path: API_KEY
"""
        store = _build_store(config_dir, workspace_dir, yaml_text)
        store.set("llm.api_key", "sk-env")
        env_text = (config_dir / ".env").read_text(encoding="utf-8")
        assert "API_KEY" in env_text
        assert store.get("llm.api_key") == "sk-env"


class TestFileMedium:
    def test_yaml_field(self, secret_home):
        config_dir, workspace_dir = secret_home
        (config_dir / "config.yaml").write_text("channels: {}\n", encoding="utf-8")
        yaml_text = """
channel.feishu.app_secret:
  medium: file
  path: config.yaml
  field: channels.feishu.app_secret
  format: yaml
"""
        store = _build_store(config_dir, workspace_dir, yaml_text)
        store.set("channel.feishu.app_secret", "sec-123")
        data = yaml.safe_load((config_dir / "config.yaml").read_text(encoding="utf-8"))
        assert data["channels"]["feishu"]["app_secret"] == "sec-123"
        assert store.get("channel.feishu.app_secret") == "sec-123"

    def test_workspace_json_field(self, secret_home):
        config_dir, workspace_dir = secret_home
        skills_dir = workspace_dir / "agent" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "skills_state.json").write_text("{}", encoding="utf-8")
        yaml_text = """
skills.clawhub.token:
  medium: file
  path: workspace/agent/skills/skills_state.json
  field: clawhub.token
  format: json
"""
        store = _build_store(config_dir, workspace_dir, yaml_text)
        store.set("skills.clawhub.token", "tok-abc")
        data = json.loads((skills_dir / "skills_state.json").read_text(encoding="utf-8"))
        assert data["clawhub"]["token"] == "tok-abc"


class TestCustomCrypto:
    def test_sensitive_key_auto_encrypt(self, secret_home):
        config_dir, workspace_dir = secret_home
        store = _build_store(config_dir, workspace_dir)
        mock = MagicMock()
        mock.encrypt.side_effect = lambda p, **_: f"ENC({p})"
        mock.decrypt.side_effect = lambda c, **_: c[4:-1]
        store.register_custom_crypto(mock)
        store.set("llm.api_key", "sk-secret")
        raw = json.loads((config_dir / "secrets_store.json").read_text(encoding="utf-8"))["llm.api_key"]
        assert raw == "ENC(sk-secret)"
        assert store.get("llm.api_key") == "sk-secret"

    def test_non_sensitive_plain(self, secret_home):
        config_dir, workspace_dir = secret_home
        store = _build_store(config_dir, workspace_dir)
        mock = MagicMock()
        store.register_custom_crypto(mock)
        store.set("my_app.title", "hello")
        raw = json.loads((config_dir / "secrets_store.json").read_text(encoding="utf-8"))["my_app.title"]
        assert raw == "hello"
        mock.encrypt.assert_not_called()


class TestAes256Gcm:
    def test_envelope_roundtrip(self, secret_home, monkeypatch):
        config_dir, workspace_dir = secret_home
        key = os.urandom(32)
        monkeypatch.setenv("JIUWEN_SECRET_MASTER_KEY", base64.b64encode(key).decode("ascii"))
        store = _build_store(config_dir, workspace_dir)
        store.configure_aes256gcm()
        store.set("vault.key", "plain", algorithm="aes256gcm")
        stored = json.loads((config_dir / "secrets_store.json").read_text(encoding="utf-8"))["vault.key"]
        assert stored.startswith("ENC:v1:aes256gcm:")
        assert store.get("vault.key") == "plain"

    def test_unconfigured_algorithm_raises(self, secret_home):
        config_dir, workspace_dir = secret_home
        store = _build_store(config_dir, workspace_dir)
        with pytest.raises(ValueError, match="not configured"):
            store.set("x.api_key", "v", algorithm="aes256gcm")


class TestRegistryValidation:
    def test_invalid_medium(self, secret_home):
        config_dir, workspace_dir = secret_home
        (config_dir / "secret_registry.yaml").write_text(
            "k:\n  medium: json\n  path: x\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="invalid medium"):
            SecretRegistry(config_dir=config_dir, workspace_dir=workspace_dir, bundled_path=Path("__missing__"))

    def test_env_with_field_rejected(self, secret_home):
        config_dir, workspace_dir = secret_home
        (config_dir / "secret_registry.yaml").write_text(
            "k:\n  medium: env\n  path: API_KEY\n  field: x\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="must not use field"):
            SecretRegistry(config_dir=config_dir, workspace_dir=workspace_dir, bundled_path=Path("__missing__"))


class TestLegacyEnvName:
    def test_env_legacy_name_uses_path(self, secret_home):
        config_dir, workspace_dir = secret_home
        (config_dir / ".env").write_text('API_KEY="ENC(cipher)"\n', encoding="utf-8")
        yaml_text = """
llm.api_key:
  medium: env
  path: API_KEY
"""
        store = _build_store(config_dir, workspace_dir, yaml_text)
        mock = MagicMock()
        mock.decrypt.return_value = "sk-decrypted"
        store.register_custom_crypto(mock)
        assert store.get("llm.api_key") == "sk-decrypted"
        mock.decrypt.assert_called_once_with("ENC(cipher)")
