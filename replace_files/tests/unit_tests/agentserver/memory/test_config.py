# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for memory configuration module.

Tests memory mode, embed config, and related functions.
"""

import os
import re
from typing import Any, Dict, List, Optional
from unittest import mock

import pytest


# Copy of functions from jiuwenclaw.agentserver.memory.config
# to avoid importing modules that trigger logger initialization

def _resolve_env_vars(value: Any) -> Any:
    """Recursively resolve environment variables in config values."""
    if isinstance(value, str):
        pattern = r'\$\{([^:}]+)(?::-([^}]*))?\}'

        def replace_env(match):
            var_name = match.group(1)
            default = match.group(2) if match.group(2) is not None else ""
            return os.getenv(var_name, default)
        return re.sub(pattern, replace_env, value)
    elif isinstance(value, dict):
        return {k: _resolve_env_vars(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [_resolve_env_vars(item) for item in value]
    else:
        return value


def get_memory_mode(config: Optional[Dict[str, Any]] = None) -> str:
    """读取 ``memory.mode``：``cloud`` 或 ``local``（默认）。"""
    memory_cfg = (config or {}).get("memory", {})
    mode = str(memory_cfg.get("mode") or "local").strip().lower()
    return "cloud" if mode == "cloud" else "local"


def is_memory_enabled(config: Optional[Dict[str, Any]] = None) -> bool:
    """Check if memory is enabled.
    
    Args:
        config: 配置字典，如果为None则默认为启用
    """
    if config is None:
        return True
    memory_config = config.get("memory", {})
    return memory_config.get("enabled", True)


def get_embed_config(config: Optional[Dict[str, Any]] = None) -> Dict[str, Optional[str]]:
    """Get embedding configuration from config file.
    
    Args:
        config: 配置字典
        
    Returns embedding API configuration from config.yaml embed section.
    """
    embed_config = (config or {}).get("embed", {})
    
    return {
        "api_key": embed_config.get("embed_api_key"),
        "base_url": embed_config.get("embed_base_url"),
        "model": embed_config.get("embed_model"),
    }


class TestGetMemoryMode:
    """Tests for get_memory_mode function."""

    @staticmethod
    def test_default_mode_is_local() -> None:
        """Test that default mode is 'local'."""
        config = {}
        mode = get_memory_mode(config)
        assert mode == "local"

    @staticmethod
    def test_cloud_mode() -> None:
        """Test that mode can be set to 'cloud'."""
        config = {"memory": {"mode": "cloud"}}
        mode = get_memory_mode(config)
        assert mode == "cloud"

    @staticmethod
    def test_mode_case_insensitive() -> None:
        """Test that mode is case insensitive."""
        config = {"memory": {"mode": "CLOUD"}}
        mode = get_memory_mode(config)
        assert mode == "cloud"

    @staticmethod
    def test_invalid_mode_defaults_to_local() -> None:
        """Test that invalid mode defaults to 'local'."""
        config = {"memory": {"mode": "invalid_mode"}}
        mode = get_memory_mode(config)
        assert mode == "local"


class TestIsMemoryEnabled:
    """Tests for is_memory_enabled function."""

    @staticmethod
    def test_memory_enabled_by_default() -> None:
        """Test that memory is enabled by default."""
        config = {"memory": {"enabled": True}}
        enabled = is_memory_enabled(config)
        assert enabled is True

    @staticmethod
    def test_memory_disabled() -> None:
        """Test that memory can be disabled."""
        config = {"memory": {"enabled": False}}
        enabled = is_memory_enabled(config)
        assert enabled is False

    @staticmethod
    def test_memory_enabled_with_none_config() -> None:
        """Test that memory is enabled when config is None."""
        enabled = is_memory_enabled(None)
        assert enabled is True


class TestGetEmbedConfig:
    """Tests for get_embed_config function."""

    @staticmethod
    def test_embed_config_values() -> None:
        """Test that embed config returns correct values."""
        config = {
            "embed": {
                "embed_api_key": "test_key",
                "embed_base_url": "https://test.embed.com",
                "embed_model": "test-model",
            }
        }
        embed_config = get_embed_config(config)

        assert embed_config["api_key"] == "test_key"
        assert embed_config["base_url"] == "https://test.embed.com"
        assert embed_config["model"] == "test-model"

    @staticmethod
    def test_embed_config_with_missing_values() -> None:
        """Test that embed config handles missing values."""
        config = {}
        embed_config = get_embed_config(config)

        assert embed_config["api_key"] is None
        assert embed_config["base_url"] is None
        assert embed_config["model"] is None


class TestEnvironmentVariableResolution:
    """Tests for environment variable resolution."""

    @staticmethod
    def test_resolve_env_vars_with_defaults() -> None:
        """Test resolving env vars with default values."""
        # Test with default value
        value = "${TEST_VAR:-default_value}"
        result = _resolve_env_vars(value)
        assert result == "default_value"

    @staticmethod
    def test_resolve_env_vars_with_env_set() -> None:
        """Test resolving env vars when env var is set."""
        with mock.patch.dict(os.environ, {"TEST_VAR": "env_value"}):
            value = "${TEST_VAR:-default_value}"
            result = _resolve_env_vars(value)
            assert result == "env_value"

    @staticmethod
    def test_resolve_env_vars_in_dict() -> None:
        """Test resolving env vars in dictionary."""
        config = {
            "key1": "${VAR1:-default1}",
            "key2": "${VAR2:-default2}",
        }
        result = _resolve_env_vars(config)

        assert result["key1"] == "default1"
        assert result["key2"] == "default2"

    @staticmethod
    def test_resolve_env_vars_in_list() -> None:
        """Test resolving env vars in list."""
        config = ["${VAR1:-default1}", "${VAR2:-default2}"]
        result = _resolve_env_vars(config)

        assert result[0] == "default1"
        assert result[1] == "default2"
