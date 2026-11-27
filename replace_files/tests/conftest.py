# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Pytest configuration and shared fixtures."""

import os
import sys
import tempfile
import types
from contextvars import ContextVar
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Generator
from unittest.mock import MagicMock

import pytest

# Mock fastmcp for CI compatibility (rich 14.x removed tracebacks_max_frames)
mock_fastmcp = MagicMock()
mock_fastmcp.FastMCP = MagicMock
sys.modules["fastmcp"] = mock_fastmcp
sys.modules["fastmcp.client"] = MagicMock()
sys.modules["fastmcp.utilities"] = MagicMock()
sys.modules["fastmcp.utilities.logging"] = MagicMock()


def _stdlib_bz2_available() -> bool:
    """Return whether this Python build includes the native _bz2 extension."""
    try:
        import bz2  # noqa: F401
    except ModuleNotFoundError as exc:
        if exc.name == "_bz2":
            return False
        raise
    return True


def _install_openjiuwen_deepsearch_stubs() -> None:
    """Stub deepsearch imports for tests that do not exercise deepsearch itself.

    Some CI Python builds miss the native _bz2 extension, which makes importing
    networkx fail through openjiuwen_deepsearch during pytest collection. These
    stubs keep unrelated tests isolated from that optional-heavy dependency.
    """

    def ensure_package(name: str) -> types.ModuleType:
        module = sys.modules.get(name)
        if module is None:
            module = types.ModuleType(name)
            module.__path__ = []
            sys.modules[name] = module
        return module

    for package_name in [
        "openjiuwen_deepsearch",
        "openjiuwen_deepsearch.config",
        "openjiuwen_deepsearch.framework",
        "openjiuwen_deepsearch.framework.openjiuwen",
        "openjiuwen_deepsearch.framework.openjiuwen.agent",
        "openjiuwen_deepsearch.utils",
        "openjiuwen_deepsearch.utils.log_utils",
    ]:
        ensure_package(package_name)

    config_module = types.ModuleType("openjiuwen_deepsearch.config.config")

    class Config:
        def __init__(self):
            self.agent_config = SimpleNamespace(
                model_dump=lambda: {
                    "llm_config": {"general": {}},
                    "web_search_engine_config": {},
                }
            )

    config_module.Config = Config
    sys.modules[config_module.__name__] = config_module

    method_module = types.ModuleType("openjiuwen_deepsearch.config.method")

    class ExecutionMethod(str, Enum):
        DEPENDENCY_DRIVING = "dependency_driving"
        PARALLEL = "parallel"

    method_module.ExecutionMethod = ExecutionMethod
    sys.modules[method_module.__name__] = method_module

    agent_factory_module = types.ModuleType(
        "openjiuwen_deepsearch.framework.openjiuwen.agent.agent_factory"
    )

    class AgentFactory:
        @staticmethod
        def create_agent(*_args, **_kwargs):
            raise RuntimeError(
                "openjiuwen_deepsearch is stubbed because this Python build lacks _bz2"
            )

    agent_factory_module.AgentFactory = AgentFactory
    sys.modules[agent_factory_module.__name__] = agent_factory_module

    workflow_module = types.ModuleType(
        "openjiuwen_deepsearch.framework.openjiuwen.agent.workflow"
    )

    def parse_endnode_content(_chunk_content):
        return None

    workflow_module.parse_endnode_content = parse_endnode_content
    sys.modules[workflow_module.__name__] = workflow_module

    log_common_module = types.ModuleType(
        "openjiuwen_deepsearch.utils.log_utils.log_common"
    )
    log_common_module.session_id_ctx = ContextVar("session_id", default="")
    sys.modules[log_common_module.__name__] = log_common_module

    log_manager_module = types.ModuleType(
        "openjiuwen_deepsearch.utils.log_utils.log_manager"
    )

    class LogManager:
        _SAFE_BASE = ""
        _initialized = False

        @classmethod
        def init(cls, *_args, **_kwargs):
            cls._initialized = True

    log_manager_module.LogManager = LogManager
    sys.modules[log_manager_module.__name__] = log_manager_module


if not _stdlib_bz2_available():
    _install_openjiuwen_deepsearch_stubs()


def pytest_configure(config):
    """Register custom marks."""
    config.addinivalue_line("markers", "asyncio: mark test as async test")


@pytest.fixture
def temp_workspace() -> Generator[Path, None, None]:
    """Create a temporary workspace directory for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        # Create basic structure
        (workspace / "config").mkdir(parents=True, exist_ok=True)
        (workspace / "workspace").mkdir(parents=True, exist_ok=True)
        (workspace / "workspace" / "agent").mkdir(parents=True, exist_ok=True)
        (workspace / "workspace" / "agent" / "skills").mkdir(parents=True, exist_ok=True)
        (workspace / "logs").mkdir(parents=True, exist_ok=True)

        yield workspace


@pytest.fixture
def temp_config_file(temp_workspace: Path) -> Generator[Path, None, None]:
    """Create a temporary config.yaml file."""
    config_content = """
# Test configuration
model:
  provider: "test_provider"
  name: "test_model"
  api_base: "https://test.api.com"
  api_key: "${TEST_API_KEY:-default_key}"

channels:
  web:
    enabled: true

evolution:
  enabled: true
  auto_scan: false
  skill_base_dir: "workspace/agent/skills"

heartbeat:
  every: "30 * * * *"
  target: "web"
"""
    config_file = temp_workspace / "config" / "config.yaml"
    config_file.write_text(config_content, encoding="utf-8")
    yield config_file


@pytest.fixture
def mock_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set up mock environment variables."""
    monkeypatch.setenv("MODEL_PROVIDER", "test_provider")
    monkeypatch.setenv("MODEL_NAME", "test_model")
    monkeypatch.setenv("API_BASE", "https://test.api.com")
    monkeypatch.setenv("API_KEY", "test_api_key")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")


@pytest.fixture
def sample_skill_md(temp_workspace: Path) -> Path:
    """Create a sample SKILL.md file."""
    skills_dir = temp_workspace / "workspace" / "agent" / "skills"
    skill_dir = skills_dir / "test-skill"
    skill_dir.mkdir(parents=True, exist_ok=True)

    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text("""---
name: test-skill
description: A test skill for unit testing
version: 1.0.0
author: Test Author
tags: [test]
---

# Test Skill

This is a test skill for unit testing purposes.

## Instructions

- Use this skill for testing
- Follow the examples below

## Examples

### Example 1: Basic usage

Input: "test"
Output: "test result"

## Troubleshooting

### Common issues

- Issue: Test fails
  Solution: Check configuration
""", encoding="utf-8")

    return skill_md


@pytest.fixture
def sample_messages():
    """Sample message list for testing signal detection."""
    return [
        {
            "role": "user",
            "content": "Help me with a task",
        },
        {
            "role": "assistant",
            "content": "I'll help you with that task",
            "tool_calls": [
                {
                    "name": "file.read",
                    "arguments": '{"file_path": "/path/to/test-skill/SKILL.md"}',
                }
            ],
        },
        {
            "role": "tool",
            "content": "Error: File not found",
            "name": "file.read",
        },
        {
            "role": "user",
            "content": "不对，应该这样做",
        },
    ]
