from __future__ import annotations

import os

import pytest

from jiuwenswarm.agents.harness.common.tools import command_tools


_ENV_KEYS = (
    "BASH_TOOL_HEAD_RATIO",
    "POWER_SHELL_TOOL_HEAD_RATIO",
    "BASH_TOOL_MAX_OUTPUT_CHARS",
    "POWER_SHELL_TOOL_MAX_OUTPUT_CHARS",
)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_exports_head_ratio_and_max_chars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(command_tools, "_get_shell_output_config", lambda: (8000, 0.5))

    command_tools.apply_shell_output_env_defaults()

    assert os.environ["BASH_TOOL_HEAD_RATIO"] == "0.5"
    assert os.environ["POWER_SHELL_TOOL_HEAD_RATIO"] == "0.5"
    assert os.environ["BASH_TOOL_MAX_OUTPUT_CHARS"] == "8000"
    assert os.environ["POWER_SHELL_TOOL_MAX_OUTPUT_CHARS"] == "8000"


def test_zero_max_chars_means_no_limit_and_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(command_tools, "_get_shell_output_config", lambda: (0, 0.6))

    command_tools.apply_shell_output_env_defaults()

    assert os.environ["BASH_TOOL_HEAD_RATIO"] == "0.6"
    assert os.environ["POWER_SHELL_TOOL_HEAD_RATIO"] == "0.6"
    assert "BASH_TOOL_MAX_OUTPUT_CHARS" not in os.environ
    assert "POWER_SHELL_TOOL_MAX_OUTPUT_CHARS" not in os.environ


def test_existing_env_wins_over_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BASH_TOOL_HEAD_RATIO", "0.9")
    monkeypatch.setattr(command_tools, "_get_shell_output_config", lambda: (20000, 0.5))

    command_tools.apply_shell_output_env_defaults()

    assert os.environ["BASH_TOOL_HEAD_RATIO"] == "0.9"
    assert os.environ["POWER_SHELL_TOOL_HEAD_RATIO"] == "0.5"
