# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Pytest configuration and shared fixtures for system tests."""

import os
import sys
import tempfile
from pathlib import Path
from typing import Generator
from unittest.mock import patch

import pytest


# Add parent directory to path for imports
sys.path.append(str(Path(__file__).parent.parent.parent))


@pytest.fixture
def temp_home() -> Generator[Path, None, None]:
    """Create a temporary HOME directory for isolated testing.

    This fixture creates a temporary directory that can be used as HOME
    for testing initialization without affecting the user's actual home directory.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        home = Path(tmpdir)
        yield home


@pytest.fixture
def clean_environment(temp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Set up a clean environment for testing initialization.

    This fixture:
    1. Overrides HOME to use a temporary directory
    2. Clears any cached configuration
    3. Resets module-level caches in utils.py
    4. Uses set_user_home to set custom home directory

    Use this fixture when testing initialization to ensure tests are isolated.
    """
    # Import before monkeypatching to get reference
    import jiuwenclaw.utils as utils_module

    # Override HOME to use temporary directory
    monkeypatch.setenv("HOME", str(temp_home))
    # Clear any cached configuration
    monkeypatch.delenv("JIUWENCLAW_CONFIG_DIR", raising=False)

    # Use set_user_home to set custom home directory
    utils_module.set_user_home(temp_home)

    # Reset cache variables
    monkeypatch.setattr(utils_module, "_initialized", False)
    monkeypatch.setattr(utils_module, "_config_dir", None)
    monkeypatch.setattr(utils_module, "_workspace_dir", None)
    monkeypatch.setattr(utils_module, "_root_dir", None)
    monkeypatch.setattr(utils_module, "_is_package", None)


@pytest.fixture
def mock_package_resources(monkeypatch: pytest.MonkeyPatch, temp_home: Path) -> None:
    """Mock package resources directory for testing.

    This fixture creates a mock resources directory structure
    to test file copying during initialization.
    """
    # Create mock package directory
    package_dir = temp_home / "mock_jiuwenclaw"
    package_dir.mkdir(parents=True, exist_ok=True)

    resources_dir = package_dir / "resources"
    resources_dir.mkdir(parents=True, exist_ok=True)

    # Create mock agent templates
    agent_dir = resources_dir / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)

    (agent_dir / "PRINCIPLE_ZH.md").write_text("# Principle (ZH)", encoding="utf-8")
    (agent_dir / "PRINCIPLE_EN.md").write_text("# Principle (EN)", encoding="utf-8")
    (agent_dir / "TONE_ZH.md").write_text("# Tone (ZH)", encoding="utf-8")
    (agent_dir / "TONE_EN.md").write_text("# Tone (EN)", encoding="utf-8")
    (agent_dir / "HEARTBEAT_ZH.md").write_text("# Heartbeat (ZH)", encoding="utf-8")
    (agent_dir / "HEARTBEAT_EN.md").write_text("# Heartbeat (EN)", encoding="utf-8")

    # Create mock config.yaml
    (resources_dir / "config.yaml").write_text("""
# Test configuration
preferred_language: zh
models:
  default:
    model_client_config:
      api_base: "${API_BASE}"
      api_key: "${API_KEY}"
      model_name: "${MODEL_NAME:-test-model}"
      client_provider: "${MODEL_PROVIDER}"
""", encoding="utf-8")

    # Create mock .env.template
    (resources_dir / ".env.template").write_text("""
API_BASE="https://test.api.com"
API_KEY="test-key"
MODEL_NAME="test-model"
MODEL_PROVIDER="OpenAI"
""", encoding="utf-8")

    # Create mock skills_state.json
    (resources_dir / "skills_state.json").write_text('{"installed": []}', encoding="utf-8")

    # Patch _find_package_root to return mock directory
    def mock_find_package_root():
        return package_dir

    monkeypatch.setattr("jiuwenclaw.utils._find_package_root", mock_find_package_root)


@pytest.fixture
def original_home(monkeypatch: pytest.MonkeyPatch) -> Path:
    """Store and restore original HOME directory.

    This fixture saves the original HOME directory and restores it
    after the test. Use this when you need to temporarily modify HOME
    but want to ensure it's restored after the test.
    """
    original_home = Path.home()

    yield original_home

    # Restore original HOME
    monkeypatch.setenv("HOME", str(original_home))


@pytest.fixture
def skip_if_no_resources():
    """Skip test if package resources are not available.

    Use this fixture for tests that require the actual package resources
    to be available (e.g., when testing in development mode).
    """
    from jiuwenclaw.utils import _find_package_root

    package_root = _find_package_root()
    resources_dir = package_root / "resources"

    if not resources_dir.exists():
        pytest.skip("Package resources not available")


@pytest.fixture
def verify_workspace_structure():
    """Helper function to verify workspace structure.

    Returns a function that checks if a workspace directory
    has the expected structure.

    Example:
        def test_something(verify_workspace_structure):
            workspace = Path("~/.jiuwenclaw").expanduser()
            verify_workspace_structure(workspace, language="zh")
    """

    def _verify(workspace_dir: Path, language: str = "zh") -> None:
        """Verify the workspace has the expected structure."""
        expected_dirs = [
            workspace_dir / "config",
            workspace_dir / "agent" / "home",
            workspace_dir / "agent" / "skills",
            workspace_dir / "agent" / "memory",
            workspace_dir / "agent" / "sessions",
            workspace_dir / "agent" / "workspace",
            workspace_dir / "agent" / ".checkpoint",
            workspace_dir / "agent" / ".logs",
        ]

        for dir_path in expected_dirs:
            if not dir_path.exists():
                raise FileNotFoundError(f"Directory {dir_path} does not exist")
            if not dir_path.is_dir():
                raise NotADirectoryError(f"{dir_path} is not a directory")

        # Check config files
        config_file = workspace_dir / "config" / "config.yaml"
        if not config_file.exists():
            raise FileNotFoundError("config.yaml does not exist")

        env_file = workspace_dir / "config" / ".env"
        if not env_file.exists():
            raise FileNotFoundError(".env does not exist")

        # Check agent templates
        agent_home = workspace_dir / "agent" / "home"
        expected_templates = ["PRINCIPLE.md", "TONE.md", "HEARTBEAT.md"]
        for template in expected_templates:
            template_path = agent_home / template
            if not template_path.exists():
                raise FileNotFoundError(f"Template {template} does not exist")

    return _verify
