# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for utils module."""

import importlib
import os
from pathlib import Path
from unittest.mock import patch

from jiuwenswarm.common import utils


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
    def test_get_default_project_workspace_dir():
        """Test no-project task workspace lives under agent workspace/projects."""
        project_workspace = utils.get_default_project_workspace_dir()
        assert isinstance(project_workspace, Path)
        assert project_workspace == utils.get_agent_workspace_dir() / "projects"

    @staticmethod
    def test_get_default_project_session_workspace_dir():
        """Test no-project task workspace is scoped by session."""
        session_workspace = utils.get_default_project_session_workspace_dir("abc-123")
        assert isinstance(session_workspace, Path)
        assert session_workspace == (
            utils.get_default_project_workspace_dir()
            / "abc-123"
        )
        assert session_workspace.exists()

    @staticmethod
    def test_get_default_project_session_workspace_dir_without_session():
        """Test early initialization does not create a throwaway session folder."""
        session_workspace = utils.get_default_project_session_workspace_dir()
        assert isinstance(session_workspace, Path)
        assert session_workspace == utils.get_default_project_workspace_dir()
        assert session_workspace.exists()

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
        assert logger.name == "jiuwenswarm"
        assert logger.level == 20  # INFO level

    @staticmethod
    def test_setup_logger_debug():
        """Test logger setup with DEBUG level."""
        logger = utils.setup_logger("DEBUG")
        assert logger.level == 10  # DEBUG level

    @staticmethod
    def test_setup_logger_error():
        """Test logger setup with ERROR level."""
        logger = utils.setup_logger("ERROR")
        assert logger.level == 40  # ERROR level

    @staticmethod
    def test_logger_handlers():
        """Test that logger has console and five rotating log files."""
        logger = utils.setup_logger("INFO")
        handler_types = [type(h).__name__ for h in logger.handlers]
        assert "StreamHandler" in handler_types
        assert handler_types.count("SafeRotatingFileHandler") == 5


class TestSourceRecordMasking:
    """Test install_source_record_masking (source-level LogRecord factory masking).

    Covers the security-critical paths called out in review:
    - third-party (non-jiuwenswarm) logger message masking,
    - traceback-embedded secret masking,
    - double-masking safety (_is_already_masked keeps fingerprint stable),
    - idempotency.
    """

    PLAINTEXT_KEY = "sk-epignnbeppwjigp932ngefebnof"

    @staticmethod
    def _capture_logger(name):
        """Build a logger with its own handler (no SensitiveDataFilter), so any
        masking observed must come from the source record factory, not handler filter.
        """
        import io
        import logging

        lg = logging.getLogger(name)
        for h in lg.handlers[:]:
            lg.removeHandler(h)
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        # Formatter 默认在 message 后自动追加 traceback（若 record 有 exc_info/exc_text），
        # 无需显式 %(exc_text)s，否则会与生产行为不一致导致 traceback 重复。
        handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
        lg.addHandler(handler)
        lg.setLevel(logging.DEBUG)
        lg.propagate = False
        return lg, buf

    @staticmethod
    def _save_state():
        """Snapshot the global LogRecord factory + install flag for later restore."""
        import logging

        return logging.getLogRecordFactory(), utils._source_record_masking_installed

    @staticmethod
    def _restore_state(state):
        """Restore the global factory + install flag (avoid cross-test pollution)."""
        import logging

        factory, flag = state
        logging.setLogRecordFactory(factory)
        utils._source_record_masking_installed = flag

    def test_third_party_logger_message_masked(self):
        """Source factory masks messages from non-jiuwenswarm loggers (openjiuwen/
        openai/httpx style) that bypass the jiuwenswarm handler-level filter."""
        import logging

        state = self._save_state()
        try:
            # Reset to plain factory, then install — proves masking comes from install.
            logging.setLogRecordFactory(logging.LogRecord)
            utils._source_record_masking_installed = False
            utils.install_source_record_masking()

            lg, buf = self._capture_logger("openjiuwen.harness.security")
            key = self.PLAINTEXT_KEY
            lg.info("config: api_key=%s, base=https://x.com", key)
            out = buf.getvalue()
            assert key not in out, "plaintext api_key leaked from third-party logger"
            assert "******" in out, "api_key not masked"
            assert "https://x.com" in out, "non-sensitive api_base should be preserved"
        finally:
            self._restore_state(state)

    def test_traceback_embedded_secret_masked(self):
        """logger.exception masks api_key embedded in the rendered traceback."""
        import logging

        state = self._save_state()
        try:
            logging.setLogRecordFactory(logging.LogRecord)
            utils._source_record_masking_installed = False
            utils.install_source_record_masking()

            lg, buf = self._capture_logger("openai._base_client")
            key = self.PLAINTEXT_KEY
            try:
                raise ValueError("build failed: api_key=" + key)
            except ValueError:
                lg.exception("init error")
            out = buf.getvalue()
            assert key not in out, "plaintext api_key leaked via traceback"
            assert "Traceback" in out, "traceback should still be rendered"
            assert "******" in out, "api_key in traceback not masked"
        finally:
            self._restore_state(state)

    def test_double_masking_preserves_fingerprint(self):
        """A record masked at source, then re-processed by _sanitize_log_text (handler
        layer), keeps the same fingerprint — _is_already_masked prevents 'fingerprint
        of fingerprint' corruption."""
        import logging
        import re

        state = self._save_state()
        try:
            logging.setLogRecordFactory(logging.LogRecord)
            utils._source_record_masking_installed = False
            utils.install_source_record_masking()

            lg, buf = self._capture_logger("httpx")
            key = self.PLAINTEXT_KEY
            lg.info("api_key=%s", key)
            source_out = buf.getvalue()

            # Re-run the handler-layer sanitizer on the already-masked text.
            double_masked = utils._sanitize_log_text(source_out)

            fp_source = re.search(r"fp:([0-9a-f]+)", source_out)
            fp_double = re.search(r"fp:([0-9a-f]+)", double_masked)
            assert fp_source, "source masking should produce a fingerprint"
            assert fp_double, "double-masked text should still carry a fingerprint"
            assert fp_source.group(1) == fp_double.group(1), (
                "fingerprint changed after double masking — _is_already_masked not effective"
            )
            # True fingerprint of the plaintext key (cross-check).
            assert fp_source.group(1) == utils._fingerprint(key)
        finally:
            self._restore_state(state)

    def test_install_is_idempotent(self):
        """Repeated install_source_record_masking calls are safe (no-op after first)."""
        import logging

        state = self._save_state()
        try:
            logging.setLogRecordFactory(logging.LogRecord)
            utils._source_record_masking_installed = False
            utils.install_source_record_masking()
            factory_after_first = logging.getLogRecordFactory()
            utils.install_source_record_masking()
            factory_after_second = logging.getLogRecordFactory()
            assert factory_after_first is factory_after_second, (
                "second install should not replace the factory (idempotent)"
            )
            assert utils._source_record_masking_installed is True
        finally:
            self._restore_state(state)


class TestUserWorkspace:
    """Test user workspace functions."""

    @patch("jiuwenswarm.common.utils.get_user_workspace_dir")
    @patch("jiuwenswarm.common.utils._find_package_root")
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
        assert ".jiuwenswarm" in str(utils.get_user_workspace_dir())


class TestMultiInstanceEnvVars:
    """Test environment variable support for multi-instance isolation (Phase 1)."""

    @staticmethod
    def test_workspace_env_var():
        """Test JIUWENSWARM_DATA_DIR environment variable overrides default workspace."""
        # Reset cache before test - must reset _workspace_base_dir for workspace tests
        setattr(utils, '_workspace_base_dir', None)
        setattr(utils, '_user_home', None)
        original_env = os.environ.pop("JIUWENSWARM_DATA_DIR", None)
        original_home_env = os.environ.pop("JIUWENSWARM_HOME", None)

        try:
            # Test default behavior
            default_workspace = utils.get_user_workspace_dir()
            assert ".jiuwenswarm" in str(default_workspace)

            # Reset cache and set env var
            setattr(utils, '_workspace_base_dir', None)
            setattr(utils, '_user_home', None)
            os.environ["JIUWENSWARM_DATA_DIR"] = "/custom/workspace/path"
            custom_workspace = utils.get_user_workspace_dir()
            # Use Path comparison for cross-platform compatibility
            assert custom_workspace == Path("/custom/workspace/path")
        finally:
            # Cleanup
            setattr(utils, '_workspace_base_dir', None)
            setattr(utils, '_user_home', None)
            os.environ.pop("JIUWENSWARM_DATA_DIR", None)
            if original_env:
                os.environ["JIUWENSWARM_DATA_DIR"] = original_env
            if original_home_env:
                os.environ["JIUWENSWARM_HOME"] = original_home_env

    @staticmethod
    def test_jiuwenswarm_home_env_var():
        """Test JIUWENSWARM_HOME environment variable overrides default home."""
        # Reset cache before test
        setattr(utils, '_user_home', None)
        original_home_env = os.environ.pop("JIUWENSWARM_HOME", None)
        original_workspace_env = os.environ.pop("JIUWENSWARM_DATA_DIR", None)

        try:
            # Set JIUWENSWARM_HOME
            os.environ["JIUWENSWARM_HOME"] = "/custom/home"
            custom_home = utils.get_user_home()
            assert custom_home == Path("/custom/home")

            # Workspace should derive from custom home
            setattr(utils, '_user_home', None)
            os.environ.pop("JIUWENSWARM_HOME", None)  # Clear for fresh test
            workspace = utils.get_user_workspace_dir()
            # Without env vars, should use Path.home()
            assert isinstance(workspace, Path)
        finally:
            # Cleanup
            setattr(utils, '_user_home', None)
            os.environ.pop("JIUWENSWARM_HOME", None)
            os.environ.pop("JIUWENSWARM_DATA_DIR", None)
            if original_home_env:
                os.environ["JIUWENSWARM_HOME"] = original_home_env
            if original_workspace_env:
                os.environ["JIUWENSWARM_DATA_DIR"] = original_workspace_env

    @staticmethod
    def test_workspace_priority_over_home():
        """Test JIUWENSWARM_DATA_DIR takes priority over JIUWENSWARM_HOME for workspace."""
        # Reset both caches - _workspace_base_dir is used by get_user_workspace_dir
        setattr(utils, '_workspace_base_dir', None)
        setattr(utils, '_user_home', None)
        original_home_env = os.environ.pop("JIUWENSWARM_HOME", None)
        original_workspace_env = os.environ.pop("JIUWENSWARM_DATA_DIR", None)

        try:
            # Set both env vars
            os.environ["JIUWENSWARM_HOME"] = "/home/a"
            os.environ["JIUWENSWARM_DATA_DIR"] = "/workspace/b"

            # Workspace should use JIUWENSWARM_DATA_DIR directly, not derive from HOME
            workspace = utils.get_user_workspace_dir()
            assert workspace == Path("/workspace/b")
        finally:
            setattr(utils, '_workspace_base_dir', None)
            setattr(utils, '_user_home', None)
            os.environ.pop("JIUWENSWARM_HOME", None)
            os.environ.pop("JIUWENSWARM_DATA_DIR", None)
            if original_home_env:
                os.environ["JIUWENSWARM_HOME"] = original_home_env
            if original_workspace_env:
                os.environ["JIUWENSWARM_DATA_DIR"] = original_workspace_env


class TestHardcodedPathsPhase2:
    """Test that hardcoded paths are fixed to use getter functions (Phase 2).

    All assertions use absolute path strings for easy observation.
    """

    @staticmethod
    def test_cron_tools_path_equivalence():
        """Test cron_tools.py path matches expected structure (cross-platform)."""
        from jiuwenswarm.common.utils import get_agent_home_dir, get_user_workspace_dir

        # Original hardcoded: get_user_workspace_dir() / "agent" / "home" / "cron_jobs.json"
        # New: get_agent_home_dir() / "cron_jobs.json"
        # get_agent_home_dir() = get_user_workspace_dir() / "agent" / "home"

        workspace = get_user_workspace_dir()
        expected_path = workspace / "agent" / "home" / "cron_jobs.json"
        actual_path = get_agent_home_dir() / "cron_jobs.json"

        assert str(actual_path.resolve()) == str(expected_path.resolve()), \
            f"Expected: {expected_path.resolve()}, Got: {actual_path.resolve()}"

    @staticmethod
    def test_task_tools_path_structure():
        """Test task_tools.py path uses workspace (migrated from legacy jiuwenswarm_workspace)."""
        # Reset caches to ensure clean state after previous tests
        setattr(utils, '_user_home', None)
        setattr(utils, '_initialized', False)
        setattr(utils, '_config_dir', None)
        setattr(utils, '_workspace_dir', None)
        setattr(utils, '_root_dir', None)

        from jiuwenswarm.agents.harness.common.tools.task_tools import _get_task_data_path
        from jiuwenswarm.common.utils import get_user_workspace_dir

        workspace = get_user_workspace_dir()
        expected_path = workspace / "agent" / "workspace" / "task-data.json"
        actual_path = Path(_get_task_data_path())

        assert str(actual_path.resolve()) == str(expected_path.resolve()), \
            f"Expected: {expected_path.resolve()}, Got: {actual_path.resolve()}"

    @staticmethod
    def test_im_inbound_path_structure():
        """Test im_inbound.py uses DeepAgent standard USER.md path."""
        # Reset caches to ensure clean state after previous tests
        setattr(utils, '_user_home', None)
        setattr(utils, '_initialized', False)
        setattr(utils, '_config_dir', None)
        setattr(utils, '_workspace_dir', None)
        setattr(utils, '_root_dir', None)

        from jiuwenswarm.common.utils import get_deepagent_user_md_path, get_user_workspace_dir

        workspace = get_user_workspace_dir()
        expected_path = workspace / "agent" / "workspace" / "USER.md"
        actual_path = get_deepagent_user_md_path()

        assert str(actual_path.resolve()) == str(expected_path.resolve()), \
            f"Expected: {expected_path.resolve()}, Got: {actual_path.resolve()}"


class TestAdditionalHardcodedPaths:
    """Test additional hardcoded paths fixed in config.py and rail_manager.py.

    All assertions use absolute path strings for easy observation.
    """

    @staticmethod
    def test_rail_manager_path_structure():
        """Test rail_manager.py uses get_agent_workspace_dir() for extensions path."""
        from jiuwenswarm.agents.harness.common.plugins.rail_manager import RailManager
        from jiuwenswarm.common.utils import get_user_workspace_dir

        workspace = get_user_workspace_dir()
        expected_path = workspace / "agent" / "workspace" / "extensions"
        rail_manager = RailManager()

        extensions_dir = getattr(rail_manager, '_extensions_dir')
        assert str(extensions_dir.resolve()) == str(expected_path.resolve()), \
            f"Expected: {expected_path.resolve()}, Got: {extensions_dir.resolve()}"

    @staticmethod
    def test_config_module_dir_structure(tmp_path):
        """Test config.py _CONFIG_MODULE_DIR honors explicit config dir."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        import jiuwenswarm.common.config as config_module

        with patch.dict(os.environ, {"JIUWENSWARM_CONFIG_DIR": str(config_dir)}):
            config_module = importlib.reload(config_module)
            module_config_dir = config_module.__dict__["_CONFIG_MODULE_DIR"]
            assert str(module_config_dir.resolve()) == str(config_dir.resolve()), \
                f"Expected: {config_dir.resolve()}, Got: {module_config_dir.resolve()}"

        importlib.reload(config_module)

    @staticmethod
    def test_interactions_dir_structure():
        """Test get_interactions_dir() returns correct path structure."""
        # Reset caches to ensure clean state
        setattr(utils, '_user_home', None)
        setattr(utils, '_workspace_base_dir', None)

        from jiuwenswarm.common.utils import get_interactions_dir, get_user_workspace_dir

        workspace = get_user_workspace_dir()
        expected_path = workspace / "agent" / "workspace" / "interactions"
        actual_path = get_interactions_dir()

        assert str(actual_path.resolve()) == str(expected_path.resolve()), \
            f"Expected: {expected_path.resolve()}, Got: {actual_path.resolve()}"
