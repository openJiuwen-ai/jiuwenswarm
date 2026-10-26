# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for /statusline prompt-type command handling.

Tests cover:
- _STATUSLINE_PROMPT_REGEX: regex matching
- _handle_statusline_prompt_command: regex matching, known
  subcommand exclusion, description extraction, prompt text content
- build_user_prompt: full pipeline (statusline-setup prompt
  injection, normal messages unaffected)
- _STATUSLINE_SETUP_PROMPT: hardcoded prompt content validation

This follows Claude Code's pattern: /statusline is a prompt-type
command that injects a hardcoded statusline-setup instruction text
directly into the user prompt — no /skills command or SkillUseRail
involved.
"""

from __future__ import annotations

import json

import pytest

from jiuwenswarm.server.runtime.agent_adapter.interface import (
    _STATUSLINE_KNOWN_SUBCOMMANDS,
    _STATUSLINE_PROMPT_REGEX,
    _STATUSLINE_SETUP_PROMPT,
    _handle_statusline_prompt_command,
    build_user_prompt,
)


def _extract_json_from_prompt(prompt: str) -> dict:
    """Extract the first JSON object from a build_user_prompt result.

    The prompt format is:
      prefix_text + JSON_string + [optional statusline-setup
      prompt suffix] The JSON ends at the closing brace before
      the suffix.
    """
    json_start = prompt.index("{")
    brace_depth = 0
    for i in range(json_start, len(prompt)):
        if prompt[i] == "{":
            brace_depth += 1
        elif prompt[i] == "}":
            brace_depth -= 1
            if brace_depth == 0:
                json_str = prompt[json_start:i + 1]
                return json.loads(json_str)
    raise ValueError("No complete JSON object found in prompt")


# ── _STATUSLINE_PROMPT_REGEX ────────────────────────────────────


class TestStatuslinePromptRegex:
    """Tests for _STATUSLINE_PROMPT_REGEX."""

    @staticmethod
    def test_matches_statusline_with_description():
        m = _STATUSLINE_PROMPT_REGEX.match(
            "/statusline show model and tokens"
        )
        assert m is not None
        assert m.group("description") == "show model and tokens"

    @staticmethod
    def test_matches_statusline_with_chinese():
        m = _STATUSLINE_PROMPT_REGEX.match(
            "/statusline 展示模型名称和token数量"
        )
        assert m is not None
        assert m.group("description") == "展示模型名称和token数量"

    @staticmethod
    def test_no_match_bare_statusline():
        """'/statusline' with no trailing description should NOT match."""
        m = _STATUSLINE_PROMPT_REGEX.match("/statusline")
        assert m is None

    @staticmethod
    def test_no_match_other_slash_command():
        m = _STATUSLINE_PROMPT_REGEX.match(
            "/skills use script-creator, hello"
        )
        assert m is None

    @staticmethod
    def test_no_match_plain_text():
        m = _STATUSLINE_PROMPT_REGEX.match("just a normal message")
        assert m is None


# ── _handle_statusline_prompt_command ──────────────────────────


class TestHandleStatuslinePromptCommand:
    """Tests for _handle_statusline_prompt_command.

    This function returns (statusline_prompt_text, description)
    when matched, or ("", original_query) when not matched.
    """

    # --- Matching cases (should return non-empty prompt text) ---

    @staticmethod
    def test_chinese_prompt():
        prompt_text, content = _handle_statusline_prompt_command(
            "/statusline 展示模型名称和使用的token数量"
        )
        assert prompt_text == _STATUSLINE_SETUP_PROMPT
        assert content == "展示模型名称和使用的token数量"

    @staticmethod
    def test_english_prompt():
        prompt_text, content = _handle_statusline_prompt_command(
            "/statusline show my PS1 config"
        )
        assert prompt_text == _STATUSLINE_SETUP_PROMPT
        assert content == "show my PS1 config"

    @staticmethod
    def test_prompt_with_whitespace():
        prompt_text, content = _handle_statusline_prompt_command(
            "  /statusline   display git branch   "
        )
        assert prompt_text == _STATUSLINE_SETUP_PROMPT
        assert content == "display git branch"

    @staticmethod
    def test_prompt_starting_with_non_subcommand_word():
        """First word is NOT a known subcommand → treat as prompt."""
        prompt_text, content = _handle_statusline_prompt_command(
            "/statusline hello world"
        )
        assert prompt_text == _STATUSLINE_SETUP_PROMPT
        assert content == "hello world"

    @staticmethod
    def test_returns_same_prompt_for_all_matches():
        """All matching inputs return the same hardcoded prompt."""
        _, c1 = _handle_statusline_prompt_command("/statusline show model")
        _, c2 = _handle_statusline_prompt_command("/statusline show git branch")
        _, c3 = _handle_statusline_prompt_command("/statusline 展示磁盘")
        # All three have different content, but same prompt_text
        assert c1 != c2 != c3

    # --- Exclusion cases (known subcommands should NOT be matched) ---

    @pytest.mark.parametrize(
        # sorted：参数源是 set 字面量，迭代序随每个 xdist worker 的
        # PYTHONHASHSEED 变化 → 各 worker 收集顺序不同 → pytest-xdist
        # "Different tests were collected between gw0 and gw1" 中止。
        "subcmd", sorted(_STATUSLINE_KNOWN_SUBCOMMANDS)
    )
    @staticmethod
    def test_known_subcommands_excluded(subcmd: str):
        """Every known subcommand should NOT be treated as a prompt."""
        prompt_text, content = _handle_statusline_prompt_command(
            f"/statusline {subcmd} some_args"
        )
        assert prompt_text == ""
        assert content == f"/statusline {subcmd} some_args"

    @staticmethod
    def test_set_subcommand_with_echo_command():
        """'/statusline set echo $mode' is a subcommand, not a prompt."""
        prompt_text, content = _handle_statusline_prompt_command(
            "/statusline set 'echo $mode | $model'"
        )
        assert prompt_text == ""
        assert "/statusline set" in content

    @staticmethod
    def test_help_subcommand():
        prompt_text, content = _handle_statusline_prompt_command(
            "/statusline help"
        )
        assert prompt_text == ""

    @staticmethod
    def test_json_subcommand():
        prompt_text, content = _handle_statusline_prompt_command(
            "/statusline json"
        )
        assert prompt_text == ""

    @staticmethod
    def test_clear_subcommand():
        prompt_text, content = _handle_statusline_prompt_command(
            "/statusline clear"
        )
        assert prompt_text == ""

    @staticmethod
    def test_padding_subcommand():
        prompt_text, content = _handle_statusline_prompt_command(
            "/statusline padding 1"
        )
        assert prompt_text == ""

    @staticmethod
    def test_get_subcommand():
        """'/statusline get' is a known subcommand, not a prompt."""
        prompt_text, content = _handle_statusline_prompt_command(
            "/statusline get"
        )
        assert prompt_text == ""

    # --- Non-matching cases (should return empty prompt_text) ---

    @staticmethod
    def test_non_statusline_input():
        prompt_text, content = _handle_statusline_prompt_command(
            "hello world"
        )
        assert prompt_text == ""
        assert content == "hello world"

    @staticmethod
    def test_other_slash_command():
        prompt_text, content = _handle_statusline_prompt_command(
            "/skills use something"
        )
        assert prompt_text == ""

    @staticmethod
    def test_empty_string():
        prompt_text, content = _handle_statusline_prompt_command("")
        assert prompt_text == ""
        assert content == ""

    @staticmethod
    def test_bare_statusline_no_args():
        """'/statusline' without description → no prompt."""
        prompt_text, content = _handle_statusline_prompt_command(
            "/statusline"
        )
        assert prompt_text == ""
        assert content == "/statusline"


# ── _STATUSLINE_SETUP_PROMPT (hardcoded prompt content) ─────────


class TestStatuslineSetupPrompt:
    """Verify _STATUSLINE_SETUP_PROMPT contains all necessary instructions."""

    @staticmethod
    def test_contains_setup_agent_identity():
        assert "status line setup agent" in _STATUSLINE_SETUP_PROMPT

    @staticmethod
    def test_contains_json_field_table():
        assert "usage.total_tokens" in _STATUSLINE_SETUP_PROMPT
        assert "model" in _STATUSLINE_SETUP_PROMPT
        assert "mode" in _STATUSLINE_SETUP_PROMPT
        assert "context_window.used_percentage" in _STATUSLINE_SETUP_PROMPT

    @staticmethod
    def test_contains_config_json_instructions():
        assert "config.json" in _STATUSLINE_SETUP_PROMPT
        assert "jiuwenswarm-tui" in _STATUSLINE_SETUP_PROMPT
        assert "statusLine" in _STATUSLINE_SETUP_PROMPT

    @staticmethod
    def test_contains_three_command_styles():
        assert "Style A" in _STATUSLINE_SETUP_PROMPT
        assert "Style B" in _STATUSLINE_SETUP_PROMPT
        assert "Style C" in _STATUSLINE_SETUP_PROMPT

    @staticmethod
    def test_contains_shell_command_examples():
        assert "jq" in _STATUSLINE_SETUP_PROMPT
        assert "git" in _STATUSLINE_SETUP_PROMPT
        assert "input=$(cat)" in _STATUSLINE_SETUP_PROMPT

    @staticmethod
    def test_contains_safety_guidelines():
        assert "2>/dev/null" in _STATUSLINE_SETUP_PROMPT

    @staticmethod
    def test_contains_auto_update_note():
        assert "2 seconds" in _STATUSLINE_SETUP_PROMPT


# ── build_user_prompt (full pipeline) ───────────────────────────


class TestBuildUserPromptStatusline:
    """Tests for build_user_prompt with /statusline prompt-type injection."""

    @staticmethod
    def test_statusline_prompt_injects_setup_instructions_zh():
        """Chinese /statusline prompt should have instructions injected."""
        prompt = build_user_prompt(
            "/statusline 展示模型名称和token数量",
            files={},
            channel="tui",
            language="zh",
        )
        # statusline-setup instructions should be present
        assert "status line setup agent" in prompt
        assert "jq" in prompt
        assert "config.json" in prompt
        # The instruction suffix in Chinese
        assert "你必须按照以下指令配置状态栏" in prompt
        # The user's description should be in the prompt content
        prompt_data = _extract_json_from_prompt(prompt)
        assert "展示模型名称和token数量" in prompt_data.get(
            "content", ""
        )
        # The /statusline prefix should be stripped from content
        assert "/statusline" not in prompt_data.get("content", "")

    @staticmethod
    def test_statusline_prompt_injects_setup_instructions_en():
        """English /statusline prompt should have instructions injected."""
        prompt = build_user_prompt(
            "/statusline show model and tokens",
            files={},
            channel="tui",
            language="en",
        )
        assert "status line setup agent" in prompt
        assert "jq" in prompt
        assert (
            "You must follow these instructions "
            "to configure the status line" in prompt
        )
        prompt_data = _extract_json_from_prompt(prompt)
        assert "show model and tokens" in prompt_data.get(
            "content", ""
        )

    @staticmethod
    def test_normal_message_no_injection():
        """Normal messages (not /statusline) should NOT have any injection."""
        prompt = build_user_prompt(
            "你好，帮我写个脚本",
            files={},
            channel="tui",
            language="zh",
        )
        assert "status line setup agent" not in prompt
        assert "状态栏" not in prompt
        assert "你必须按照" not in prompt

    @staticmethod
    def test_skills_use_command_no_statusline_conflict():
        """'/skills use' should be handled by its own handler."""
        prompt = build_user_prompt(
            "/skills use script-creator, show tokens",
            files={},
            channel="tui",
            language="zh",
        )
        # Should NOT inject statusline-setup prompt
        assert "status line setup agent" not in prompt
        # Content should contain the query part
        assert "show tokens" in prompt

    @staticmethod
    def test_statusline_set_subcommand_not_injected():
        """'/statusline set' is a known subcommand → no injection."""
        prompt = build_user_prompt(
            "/statusline set 'echo $mode'",
            files={},
            channel="tui",
            language="zh",
        )
        assert "你必须按照以下指令配置状态栏" not in prompt
        assert "status line setup agent" not in prompt
        prompt_data = _extract_json_from_prompt(prompt)
        assert prompt_data.get("content") == "/statusline set 'echo $mode'"

    @staticmethod
    def test_statusline_preserves_other_prompt_features():
        """build_user_prompt should still include timestamp, channel, etc."""
        prompt = build_user_prompt(
            "/statusline show git branch",
            files={},
            channel="tui",
            language="en",
        )
        prompt_data = _extract_json_from_prompt(prompt)
        assert prompt_data.get("source") == "tui"
        assert prompt_data.get("timezone") == "Asia/Shanghai"
        assert prompt_data.get("type") == "user input"
        assert "timestamp" in prompt_data

    @staticmethod
    def test_statusline_with_files_and_trusted_dirs():
        """/statusline prompt with files and trusted_dirs should work."""
        prompt = build_user_prompt(
            "/statusline show model",
            files={"test.py": "content"},
            channel="tui",
            language="zh",
            trusted_dirs=["/home/user/project"],
        )
        assert "status line setup agent" in prompt
        prompt_data = _extract_json_from_prompt(prompt)
        assert prompt_data.get("trusted_dirs") is not None

    @staticmethod
    def test_cron_channel_prompt_injected():
        """/statusline should work even in cron channel."""
        prompt = build_user_prompt(
            "/statusline show something",
            files={},
            channel="cron",
            language="zh",
        )
        assert "status line setup agent" in prompt

    @staticmethod
    def test_no_skill_file_dependency():
        """Injection from hardcoded string, not SKILL.md — aligned with Claude Code."""
        prompt = build_user_prompt(
            "/statusline show model",
            files={},
            channel="tui",
            language="en",
        )
        # Should NOT reference skill files or skill-loading mechanisms
        assert "[Skill:" not in prompt
        assert "SkillUseRail" not in prompt
        assert "SKILL.md" not in prompt
        # Should use the hardcoded prompt text
        assert _STATUSLINE_SETUP_PROMPT in prompt


# ── Known subcommands constant ──────────────────────────────────


class TestStatuslineKnownSubcommands:
    """Verify the known subcommands set is correct."""

    @staticmethod
    def test_set_is_known():
        assert "set" in _STATUSLINE_KNOWN_SUBCOMMANDS

    @staticmethod
    def test_padding_is_known():
        assert "padding" in _STATUSLINE_KNOWN_SUBCOMMANDS

    @staticmethod
    def test_clear_is_known():
        assert "clear" in _STATUSLINE_KNOWN_SUBCOMMANDS

    @staticmethod
    def test_help_is_known():
        assert "help" in _STATUSLINE_KNOWN_SUBCOMMANDS

    @staticmethod
    def test_json_is_known():
        assert "json" in _STATUSLINE_KNOWN_SUBCOMMANDS

    @staticmethod
    def test_get_is_known():
        assert "get" in _STATUSLINE_KNOWN_SUBCOMMANDS

    @staticmethod
    def test_all_six_subcommands():
        assert len(_STATUSLINE_KNOWN_SUBCOMMANDS) == 6

    @staticmethod
    def test_unknown_word_not_in_set():
        assert "show" not in _STATUSLINE_KNOWN_SUBCOMMANDS
        assert "display" not in _STATUSLINE_KNOWN_SUBCOMMANDS
