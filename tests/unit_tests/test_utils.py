# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for utils module."""

import logging
import os
import sys
import tempfile
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
        assert logger.name == "jiuwenclaw"
        assert logger.level == 20  # INFO level

    @staticmethod
    def test_setup_logger_debug():
        """Test logger setup with DEBUG level."""
        logger = utils.setup_logger("DEBUG")
        assert logger.level == 10  # DEBUG level

    @staticmethod
    def test_setup_logger_uses_jiuwenclaw_log_level_for_file_logs():
        """Process-level trace switch must enable DEBUG on the Jiuwen root logger."""
        with patch.dict(os.environ, {"JIUWENCLAW_LOG_LEVEL": "DEBUG"}, clear=False):
            logger = utils.setup_logger()

        assert logger.level == logging.DEBUG

    @staticmethod
    def test_setup_logger_error():
        """Test logger setup with ERROR level."""
        logger = utils.setup_logger("ERROR")
        assert logger.level == 40  # ERROR level

    @staticmethod
    def test_logger_handlers():
        """Test that logger has console and five rotating log files."""
        logger = utils.setup_logger("INFO")
        # QueueHandler/QueueListener 改造后，目标 handler 在 _log_listener 上
        if utils._log_listener is not None:
            handler_types = [type(h).__name__ for h in utils._log_listener.handlers]
        else:
            handler_types = [type(h).__name__ for h in logger.handlers]
        assert "StreamHandler" in handler_types
        assert handler_types.count("SafeRotatingFileHandler") == 5

    @staticmethod
    def test_safe_rotating_file_handler_rollover():
        """Rollover copies backup and truncates active log without logging errors."""
        with tempfile.TemporaryDirectory() as tmpdir:
            logpath = Path(tmpdir) / "full.log"
            handler = utils.SafeRotatingFileHandler(
                str(logpath),
                maxBytes=80,
                backupCount=3,
                encoding="utf-8",
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
            logger = logging.getLogger("jiuwenclaw.test.rollover")
            logger.handlers.clear()
            logger.propagate = False
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)

            for _ in range(30):
                logger.info("x" * 20)

            handler.close()
            backups = list(Path(tmpdir).glob("full_*.log"))
            assert backups, "expected timestamped backup after rollover"
            assert logpath.stat().st_size < 80, "active log should be truncated after rollover"


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
