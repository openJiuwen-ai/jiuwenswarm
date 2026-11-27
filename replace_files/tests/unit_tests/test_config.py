# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for config module."""

import os
import math
from pathlib import Path

import pytest
import yaml

from jiuwenclaw.config import resolve_env_vars, get_config_raw


class TestResolveEnvVars:
    """Test environment variable resolution in config."""

    @staticmethod
    def test_resolve_string_with_env_var(monkeypatch: pytest.MonkeyPatch):
        """Test resolving string with environment variable."""
        monkeypatch.setenv("TEST_VAR", "test_value")
        result = resolve_env_vars("${TEST_VAR}")
        assert result == "test_value"

    @staticmethod
    def test_resolve_string_with_default():
        """Test resolving string with default value."""
        result = resolve_env_vars("${TEST_VAR:-default_value}")
        assert result == "default_value"

    @staticmethod
    def test_resolve_string_with_env_and_default(monkeypatch: pytest.MonkeyPatch):
        """Test resolving string when env var exists with default."""
        monkeypatch.setenv("TEST_VAR", "actual_value")
        result = resolve_env_vars("${TEST_VAR:-default_value}")
        assert result == "actual_value"

    @staticmethod
    def test_resolve_empty_string():
        """Test resolving empty string."""
        result = resolve_env_vars("")
        assert result == ""

    @staticmethod
    def test_resolve_string_without_env_var():
        """Test resolving string without environment variable syntax."""
        result = resolve_env_vars("plain_string")
        assert result == "plain_string"

    @staticmethod
    def test_resolve_dict_with_env_vars(monkeypatch: pytest.MonkeyPatch):
        """Test resolving dictionary with environment variables."""
        monkeypatch.setenv("API_KEY", "secret_key")
        monkeypatch.setenv("PORT", "8080")
        input_dict = {
            "api_key": "${API_KEY}",
            "port": "${PORT:-3000}",
            "name": "test",
        }
        result = resolve_env_vars(input_dict)
        assert result == {
            "api_key": "secret_key",
            "port": "8080",
            "name": "test",
        }

    @staticmethod
    def test_resolve_list_with_env_vars(monkeypatch: pytest.MonkeyPatch):
        """Test resolving list with environment variables."""
        monkeypatch.setenv("VAR1", "value1")
        monkeypatch.setenv("VAR2", "value2")
        input_list = [
            "${VAR1}",
            "${VAR2:-default}",
            "static_value",
        ]
        result = resolve_env_vars(input_list)
        assert result == ["value1", "value2", "static_value"]

    @staticmethod
    def test_resolve_nested_structure(monkeypatch: pytest.MonkeyPatch):
        """Test resolving nested structure with environment variables."""
        monkeypatch.setenv("HOST", "example.com")
        input_dict = {
            "server": {
                "host": "${HOST}",
                "port": "${PORT:-8080}",
            },
            "features": ["${FEATURE_A:-default_a}", "feature_b"],
        }
        result = resolve_env_vars(input_dict)
        assert result == {
            "server": {
                "host": "example.com",
                "port": "8080",
            },
            "features": ["default_a", "feature_b"],
        }

    @staticmethod
    def test_resolve_multiple_vars_in_string(monkeypatch: pytest.MonkeyPatch):
        """Test resolving multiple environment variables in one string."""
        monkeypatch.setenv("USER", "john")
        monkeypatch.setenv("DOMAIN", "example.com")
        result = resolve_env_vars("${USER}@${DOMAIN}")
        assert result == "john@example.com"

    @staticmethod
    def test_resolve_non_string_types():
        """Test that non-string types are returned as-is."""
        assert resolve_env_vars(123) == 123
        assert resolve_env_vars(True) is True
        assert resolve_env_vars(None) is None
        assert math.isclose(resolve_env_vars(3.14), 3.14)


class TestConfigFunctions:
    """Test config module functions."""

    @staticmethod
    def test_get_config_raw(temp_config_file: Path):
        """Test reading raw config without env resolution."""
        config = get_config_raw()
        assert config is not None
        assert "model" in config or "channels" in config

    @staticmethod
    def test_config_file_structure(temp_config_file: Path):
        """Test that config file has expected structure."""
        config = get_config_raw()
        # Check for common top-level keys
        expected_keys = {"model", "channels", "evolution", "heartbeat"}
        actual_keys = set(config.keys())
        assert len(actual_keys & expected_keys) > 0, "Config should have at least some expected keys"
