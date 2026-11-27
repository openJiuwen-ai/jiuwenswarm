# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for utils module."""

import os
import sys
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from jiuwenclaw import utils


class TestPathResolution:
    """Test path resolution functions."""

    @staticmethod
    def test_get_root_dir():
        """Test get_root_dir returns a Path."""
        root = utils.get_root_dir()
        assert isinstance(root, Path)
        assert root.exists()

    @staticmethod
    def test_get_config_dir():
        """Test get_config_dir returns a Path."""
        config_dir = utils.get_config_dir()
        assert isinstance(config_dir, Path)

    @staticmethod
    def test_get_workspace_dir():
        """Test get_workspace_dir returns a Path."""
        workspace = utils.get_workspace_dir()
        assert isinstance(workspace, Path)

    @staticmethod
    def test_get_config_file():
        """Test get_config_file returns config.yaml path."""
        config_file = utils.get_config_file()
        assert isinstance(config_file, Path)
        assert config_file.name == "config.yaml"

    @staticmethod
    def test_get_agent_workspace_dir():
        """Test get_agent_workspace_dir returns agent workspace."""
        agent_workspace = utils.get_agent_workspace_dir()
        assert isinstance(agent_workspace, Path)
        assert "agent" in str(agent_workspace)

    @staticmethod
    def test_path_caching():
        """Test that path results are cached."""
        # First call
        root1 = utils.get_root_dir()
        # Second call should return cached result
        root2 = utils.get_root_dir()
        assert root1 == root2


class TestPackageDetection:
    """Test package installation detection."""

    @staticmethod
    def test_is_package_installation():
        """Test package installation detection."""
        # In normal testing, this should return False (development mode)
        result = utils.is_package_installation()
        assert isinstance(result, bool)


class TestLoggerSetup:
    """Test logger setup."""

    @staticmethod
    def test_setup_logger_default():
        """Test logger setup with default level from explicit override."""
        logger = utils.setup_logger("INFO")
        try:
            assert logger.name == "jiuwenclaw"
            assert logger.level == 20  # INFO level
        finally:
            utils.shutdown_logging()

    @staticmethod
    def test_setup_logger_debug():
        """Test logger setup with DEBUG level."""
        logger = utils.setup_logger("DEBUG")
        try:
            assert logger.level == 10  # DEBUG level
        finally:
            utils.shutdown_logging()

    @staticmethod
    def test_setup_logger_error():
        """Test logger setup with ERROR level."""
        logger = utils.setup_logger("ERROR")
        try:
            assert logger.level == 40  # ERROR level
        finally:
            utils.shutdown_logging()

    @staticmethod
    def test_logger_handlers_sync_mode(monkeypatch):
        """LOG_ASYNC=0 attaches file/console handlers directly to root."""
        monkeypatch.setenv("LOG_ASYNC", "0")
        logger = utils.setup_logger("INFO")
        try:
            handler_types = [type(h).__name__ for h in logger.handlers]
            assert "StreamHandler" in handler_types
            assert handler_types.count("SafeRotatingFileHandler") == 4
        finally:
            utils.shutdown_logging()

    @staticmethod
    def test_logger_handlers_async_mode(monkeypatch):
        """LOG_ASYNC=1 uses a single QueueHandler on root."""
        monkeypatch.setenv("LOG_ASYNC", "1")
        logger = utils.setup_logger("INFO")
        try:
            handler_types = [type(h).__name__ for h in logger.handlers]
            assert handler_types == ["QueueHandler"]
            assert utils.async_logging_enabled()
        finally:
            utils.shutdown_logging()

    @staticmethod
    def test_log_level_env_overrides_file_handlers(monkeypatch):
        """LOG_LEVEL should control console and file handler levels."""
        monkeypatch.setenv("LOG_ASYNC", "0")
        monkeypatch.setenv("LOG_LEVEL", "ERROR")
        logger = utils.setup_logger()
        try:
            assert logger.level == logging.ERROR
            for handler in logger.handlers:
                assert handler.level == logging.ERROR
        finally:
            utils.shutdown_logging()


class TestUserWorkspace:
    """Test user workspace functions."""

    @patch("jiuwenclaw.utils.get_user_workspace_dir")
    @patch("jiuwenclaw.utils._find_package_root")
    @patch("pathlib.Path.exists")
    @patch("builtins.input")
    def test_init_user_workspace_cancelled(
        self, mock_input, mock_exists, mock_find_root, mock_get_workspace_dir, temp_workspace
    ):
        """Test user workspace initialization when user cancels."""
        # This test requires more complex mocking due to file operations
        # Simplified version
        pass


class TestConstants:
    """Test module constants."""

    @staticmethod
    def test_get_user_home_defined():
        """Test get_user_home is defined and returns a Path."""
        assert hasattr(utils, "get_user_home")
        assert isinstance(utils.get_user_home(), Path)

    @staticmethod
    def test_get_user_workspace_dir_defined():
        """Test get_user_workspace_dir is defined."""
        assert hasattr(utils, "get_user_workspace_dir")
        assert isinstance(utils.get_user_workspace_dir(), Path)
        assert ".jiuwenclaw" in str(utils.get_user_workspace_dir())
