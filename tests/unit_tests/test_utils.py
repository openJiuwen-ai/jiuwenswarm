# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for utils module."""

import importlib
import os
import shutil
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

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
    def test_collapse_nested_agent_workspace_dir():
        """PPT historically appended /workspace onto the agent workspace."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            agent_ws = tmp_path / "workspace_office" / "agent" / "workspace"
            nested = agent_ws / "workspace"
            nested.mkdir(parents=True)
            assert utils.collapse_nested_agent_workspace_dir(nested) == agent_ws.resolve()
            assert utils.collapse_nested_agent_workspace_dir(agent_ws) == agent_ws.resolve()
            other = tmp_path / "decks" / "demo"
            other.mkdir(parents=True)
            assert utils.collapse_nested_agent_workspace_dir(other) == other.resolve()

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
        """Root only has QueueHandler; files/console live on the listener."""
        logger = utils.setup_logger("INFO")
        root_types = [type(h).__name__ for h in logger.handlers]
        assert root_types == ["QueueHandler"]
        output_types = [type(h).__name__ for h in utils._iter_log_output_handlers()]
        assert "StreamHandler" in output_types
        assert output_types.count("SafeRotatingFileHandler") == 5


class TestSourceRecordMasking:
    """Test install_source_record_masking (source-level LogRecord factory masking).

    Covers the security-critical paths called out in review:
    - third-party (non-jiuwenswarm) logger message masking,
    - traceback-embedded secret masking,
    - double-masking safety (engine sanitize is idempotent on already-masked text),
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

    def test_double_masking_is_idempotent(self):
        """Source 脱敏后再经 ``_sanitize_log_text``（引擎）处理，明文仍被掩码且结果稳定。"""
        import logging

        state = self._save_state()
        try:
            logging.setLogRecordFactory(logging.LogRecord)
            utils._source_record_masking_installed = False
            utils.install_source_record_masking()

            lg, buf = self._capture_logger("httpx")
            key = self.PLAINTEXT_KEY
            lg.info("api_key=%s", key)
            source_out = buf.getvalue()
            assert key not in source_out
            assert "******" in source_out

            double_masked = utils._sanitize_log_text(source_out)
            assert key not in double_masked
            assert "******" in double_masked
            assert utils._sanitize_log_text(double_masked) == double_masked
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
        """Test cron_tools.py path matches expected multi-tenant structure."""
        from jiuwenswarm.common.utils import get_agent_home_dir, get_user_workspace_dir

        workspace = get_user_workspace_dir()
        expected_path = (
            workspace
            / "service_default"
            / "agent_default"
            / "agent"
            / "home"
            / "cron_jobs.json"
        )
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
        expected_path = (
            workspace
            / "service_default"
            / "agent_default"
            / "agent"
            / "jiuwenclaw_workspace"
            / "task-data.json"
        )
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
        expected_path = (
            workspace
            / "service_default"
            / "agent_default"
            / "agent"
            / "jiuwenclaw_workspace"
            / "USER.md"
        )
        actual_path = get_deepagent_user_md_path()

        assert str(actual_path.resolve()) == str(expected_path.resolve()), \
            f"Expected: {expected_path.resolve()}, Got: {actual_path.resolve()}"


class TestAdditionalHardcodedPaths:
    """Test additional hardcoded paths fixed in config.py and rail_manager.py.

    All assertions use absolute path strings for easy observation.
    """

    @staticmethod
    def test_multi_tenant_workspace_dir_edition_layout(monkeypatch, tmp_path):
        """个人版按 service_{sid}/agent_{aid} 分桶；企业版按 workspace_key 分桶。"""
        from jiuwenswarm.common.utils import get_multi_tenant_user_workspace_dir

        monkeypatch.setattr(
            "jiuwenswarm.common.utils.get_user_workspace_dir",
            lambda: tmp_path,
        )
        monkeypatch.setattr(
            "jiuwenswarm.common.utils.is_enterprise",
            lambda: False,
        )
        assert get_multi_tenant_user_workspace_dir("anything") == (
            tmp_path / "service_default" / "agent_default"
        )
        assert get_multi_tenant_user_workspace_dir(
            service_id="default",
            agent_id="office",
        ) == (tmp_path / "service_default" / "agent_office")

        monkeypatch.setattr(
            "jiuwenswarm.common.utils.is_enterprise",
            lambda: True,
        )
        assert get_multi_tenant_user_workspace_dir("abc") == (
            tmp_path / "workspace_abc"
        )

    @staticmethod
    def test_rail_manager_path_structure():
        """Test rail_manager uses multi-tenant workspace for extensions path."""
        from jiuwenswarm.agents.harness.common.plugins.rail_manager import get_rail_manager
        from jiuwenswarm.common.utils import get_multi_tenant_user_workspace_dir
        from jiuwenswarm.server.runtime.runtime_scope import RuntimeScopeKey

        scope = RuntimeScopeKey.from_ids()
        import os
        os.environ["JIUWENSWARM_EDITION"] = "enterprise"
        try:
            workspace = get_multi_tenant_user_workspace_dir(scope.workspace_key)
            expected_path = workspace / "agent" / "jiuwenclaw_workspace" / "extensions"
            rail_manager = get_rail_manager(scope)

            extensions_dir = rail_manager.extensions_dir
            assert str(extensions_dir.resolve()) == str(expected_path.resolve()), \
                f"Expected: {expected_path.resolve()}, Got: {extensions_dir.resolve()}"
        finally:
            os.environ.pop("JIUWENSWARM_EDITION", None)

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
        expected_path = (
            workspace
            / "service_default"
            / "agent_default"
            / "agent"
            / "jiuwenclaw_workspace"
            / "interactions"
        )
        actual_path = get_interactions_dir()

        assert str(actual_path.resolve()) == str(expected_path.resolve()), \
            f"Expected: {expected_path.resolve()}, Got: {actual_path.resolve()}"


class TestWorkspaceToJiuwenclawWorkspaceMigration:
    """Migrate runtime agent workspace: agent/workspace -> agent/jiuwenclaw_workspace."""

    @staticmethod
    def _make_old_layout(root: Path, tenant: str = "personal") -> Path:
        if tenant == "personal":
            agent_root = root / "service_default" / "agent_default" / "agent"
        else:
            agent_root = root / "workspace_abc" / "agent"
        old_ws = agent_root / "workspace"
        (old_ws / "skills").mkdir(parents=True)
        (old_ws / "skills" / "demo.md").write_text("skill", encoding="utf-8")
        (old_ws / "HEARTBEAT.md").write_text("heartbeat", encoding="utf-8")
        return old_ws

    def test_rename_personal_tenant(self, tmp_path: Path):
        old_ws = self._make_old_layout(tmp_path, "personal")
        utils._migrate_workspace_to_jiuwenclaw_workspace(tmp_path)

        new_ws = tmp_path / "service_default" / "agent_default" / "agent" / "jiuwenclaw_workspace"
        assert not old_ws.exists()
        assert (new_ws / "HEARTBEAT.md").read_text(encoding="utf-8") == "heartbeat"
        assert (new_ws / "skills" / "demo.md").exists()

    def test_rename_enterprise_tenant(self, tmp_path: Path):
        old_ws = self._make_old_layout(tmp_path, "enterprise")
        utils._migrate_workspace_to_jiuwenclaw_workspace(tmp_path)

        new_ws = tmp_path / "workspace_abc" / "agent" / "jiuwenclaw_workspace"
        assert not old_ws.exists()
        assert (new_ws / "HEARTBEAT.md").exists()

    def test_merge_when_both_exist(self, tmp_path: Path):
        old_ws = self._make_old_layout(tmp_path, "personal")
        new_ws = tmp_path / "service_default" / "agent_default" / "agent" / "jiuwenclaw_workspace"
        (new_ws / "memory").mkdir(parents=True)
        (new_ws / "memory" / "MEMORY.md").write_text("memory", encoding="utf-8")

        utils._migrate_workspace_to_jiuwenclaw_workspace(tmp_path)

        assert not old_ws.exists()
        # merged content from both sides
        assert (new_ws / "memory" / "MEMORY.md").read_text(encoding="utf-8") == "memory"
        assert (new_ws / "HEARTBEAT.md").read_text(encoding="utf-8") == "heartbeat"
        assert (new_ws / "skills" / "demo.md").exists()

    def test_merge_top_level_conflict(self, tmp_path: Path):
        old_ws = self._make_old_layout(tmp_path, "personal")
        agent_root = tmp_path / "service_default" / "agent_default" / "agent"
        new_ws = agent_root / "jiuwenclaw_workspace"
        new_ws.mkdir(parents=True)
        (new_ws / "USER.md").write_text("new-side", encoding="utf-8")
        (old_ws / "USER.md").write_text("old-side", encoding="utf-8")

        utils._migrate_workspace_to_jiuwenclaw_workspace(tmp_path)

        assert not old_ws.exists()
        # New side wins the conflict...
        assert (new_ws / "USER.md").read_text(encoding="utf-8") == "new-side"
        # ...and the old-side content is preserved in a backup dir, not lost.
        # Backup lives under agent/ (hidden sibling), not inside the workspace
        # where it would pollute the file tree exposed to users and the Agent.
        backups = list(agent_root.glob(".migration-backup-*/USER.md"))
        assert len(backups) == 1
        assert backups[0].read_text(encoding="utf-8") == "old-side"

    def test_merge_nested_conflict(self, tmp_path: Path):
        old_ws = self._make_old_layout(tmp_path, "personal")
        agent_root = tmp_path / "service_default" / "agent_default" / "agent"
        new_ws = agent_root / "jiuwenclaw_workspace"
        (new_ws / "memory").mkdir(parents=True)
        (new_ws / "memory" / "MEMORY.md").write_text("new-side", encoding="utf-8")
        (old_ws / "memory").mkdir()
        (old_ws / "memory" / "MEMORY.md").write_text("old-side", encoding="utf-8")
        # old-side-only nested file must still be merged in
        (old_ws / "memory" / "2026-09-07.md").write_text("daily", encoding="utf-8")

        utils._migrate_workspace_to_jiuwenclaw_workspace(tmp_path)

        assert not old_ws.exists()
        # Same priority direction as top-level: new side wins inside directories
        assert (new_ws / "memory" / "MEMORY.md").read_text(encoding="utf-8") == "new-side"
        assert (new_ws / "memory" / "2026-09-07.md").read_text(encoding="utf-8") == "daily"
        # Old-side conflicting file backed up, not overwritten/lost
        backups = list(agent_root.glob(".migration-backup-*/memory/MEMORY.md"))
        assert len(backups) == 1
        assert backups[0].read_text(encoding="utf-8") == "old-side"

    def test_noop_when_no_legacy_layout(self, tmp_path: Path):
        utils._migrate_workspace_to_jiuwenclaw_workspace(tmp_path)
        assert not (tmp_path / "jiuwenclaw_workspace").exists()

    def test_legacy_pre_tenant_layout(self, tmp_path: Path):
        old_ws = tmp_path / "agent" / "workspace"
        old_ws.mkdir(parents=True)
        (old_ws / "todo").mkdir()
        utils._migrate_workspace_to_jiuwenclaw_workspace(tmp_path)
        assert (tmp_path / "agent" / "jiuwenclaw_workspace" / "todo").exists()

    @staticmethod
    def _write_legacy_metadata(agent_root: Path, old_ws: Path) -> None:
        """Simulate pre-migration on-disk state: projects.json + session metadata
        whose project_dir strings point at the OLD absolute workspace path."""
        import json as _json

        (agent_root / "projects.json").write_text(
            _json.dumps(
                {
                    "version": 1,
                    "projects": [
                        {
                            "project_id": "p1",
                            "name": "demo",
                            "project_dir": str(old_ws / "work" / "demo"),
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        sess_dir = agent_root / "sessions" / "sess-1"
        sess_dir.mkdir(parents=True)
        (sess_dir / "metadata.json").write_text(
            _json.dumps(
                {
                    "session_id": "sess-1",
                    "project_dir": str(old_ws / "work" / "demo"),
                    "channel_metadata": {
                        "cwd": str(old_ws / "work" / "demo"),
                        "project_dir": str(old_ws / "work" / "demo"),
                    },
                }
            ),
            encoding="utf-8",
        )

    def test_migration_rewrites_persisted_project_dir(self, tmp_path: Path):
        """Regression: persisted project_dir must follow the renamed directory.

        Otherwise validate_project_dir re-creates the old path (zombie dir),
        the next restart merges it away again, and files written in between
        become invisible to the session.
        """
        import json as _json

        old_ws = self._make_old_layout(tmp_path, "personal")
        agent_root = tmp_path / "service_default" / "agent_default" / "agent"
        self._write_legacy_metadata(agent_root, old_ws)

        utils._migrate_workspace_to_jiuwenclaw_workspace(tmp_path)

        new_ws = agent_root / "jiuwenclaw_workspace"
        new_project_dir = str(new_ws / "work" / "demo")

        projects = _json.loads(
            (agent_root / "projects.json").read_text(encoding="utf-8")
        )
        assert projects["projects"][0]["project_dir"] == new_project_dir

        meta = _json.loads(
            (agent_root / "sessions" / "sess-1" / "metadata.json").read_text(
                encoding="utf-8"
            )
        )
        assert meta["project_dir"] == new_project_dir
        assert meta["channel_metadata"]["cwd"] == new_project_dir
        assert meta["channel_metadata"]["project_dir"] == new_project_dir

    def test_migration_rewrites_metadata_on_merge_too(self, tmp_path: Path):
        """Merge branch (both dirs exist) must also rewrite persisted paths."""
        import json as _json

        old_ws = self._make_old_layout(tmp_path, "personal")
        agent_root = tmp_path / "service_default" / "agent_default" / "agent"
        (agent_root / "jiuwenclaw_workspace").mkdir(parents=True)
        self._write_legacy_metadata(agent_root, old_ws)

        utils._migrate_workspace_to_jiuwenclaw_workspace(tmp_path)

        projects = _json.loads(
            (agent_root / "projects.json").read_text(encoding="utf-8")
        )
        new_ws = agent_root / "jiuwenclaw_workspace"
        assert projects["projects"][0]["project_dir"] == str(new_ws / "work" / "demo")

    def test_metadata_rewrite_idempotent(self, tmp_path: Path):
        """Re-running migration (e.g. crash-restart loop) must not corrupt metadata."""
        import json as _json

        old_ws = self._make_old_layout(tmp_path, "personal")
        agent_root = tmp_path / "service_default" / "agent_default" / "agent"
        self._write_legacy_metadata(agent_root, old_ws)

        utils._migrate_workspace_to_jiuwenclaw_workspace(tmp_path)
        # Second run: old dir gone; metadata rewrite must be a no-op, not a rewrite
        mtime_first = (agent_root / "projects.json").stat().st_mtime_ns
        utils._migrate_workspace_to_jiuwenclaw_workspace(tmp_path)

        new_ws = agent_root / "jiuwenclaw_workspace"
        projects = _json.loads(
            (agent_root / "projects.json").read_text(encoding="utf-8")
        )
        assert projects["projects"][0]["project_dir"] == str(new_ws / "work" / "demo")
        assert (agent_root / "projects.json").stat().st_mtime_ns == mtime_first

    def test_concurrent_migration_missing_old_dir_no_crash(self, tmp_path: Path):
        """Simulate another process finishing the move first: FileNotFoundError
        from move/rmtree must not crash startup; metadata still rewritten."""
        import json as _json

        old_ws = self._make_old_layout(tmp_path, "personal")
        agent_root = tmp_path / "service_default" / "agent_default" / "agent"
        self._write_legacy_metadata(agent_root, old_ws)

        # Another "process" migrates and leaves no old dir behind
        (agent_root / "jiuwenclaw_workspace").mkdir(parents=True)
        shutil.rmtree(old_ws)

        utils._migrate_workspace_to_jiuwenclaw_workspace(tmp_path)  # must not raise

        projects = _json.loads(
            (agent_root / "projects.json").read_text(encoding="utf-8")
        )
        new_ws = agent_root / "jiuwenclaw_workspace"
        assert projects["projects"][0]["project_dir"] == str(new_ws / "work" / "demo")
