# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Pytest configuration and shared fixtures."""

import os
import sys
import tempfile
from pathlib import Path
from typing import Generator

import pytest


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

react:
  evolution:
    skill_evolution: true
    auto_save: false

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
