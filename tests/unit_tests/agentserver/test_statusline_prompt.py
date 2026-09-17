# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for ``/statusline`` dispatch to the built-in setup subagent."""

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


def _extract_envelope(prompt: str) -> dict:
    return json.loads(prompt[prompt.index("{"):])


class TestStatuslinePromptRegex:
    @staticmethod
    def test_matches_statusline_with_description():
        match = _STATUSLINE_PROMPT_REGEX.match("/statusline show model and tokens")
        assert match is not None
        assert match.group("description") == "show model and tokens"

    @staticmethod
    def test_matches_chinese_description():
        match = _STATUSLINE_PROMPT_REGEX.match("/statusline 显示模型和上下文")
        assert match is not None
        assert match.group("description") == "显示模型和上下文"

    @pytest.mark.parametrize("text", ["/statusline", "/skills use demo, hello", "hello"])
    @staticmethod
    def test_does_not_match_other_inputs(text: str):
        assert _STATUSLINE_PROMPT_REGEX.match(text) is None


class TestHandleStatuslinePromptCommand:
    @pytest.mark.parametrize(
        ("query", "description"),
        [
            ("/statusline show my PS1 config", "show my PS1 config"),
            ("  /statusline   display git branch   ", "display git branch"),
            ("/statusline 显示模型和上下文", "显示模型和上下文"),
        ],
    )
    @staticmethod
    def test_builds_dedicated_subagent_dispatch(query: str, description: str):
        dispatch, content = _handle_statusline_prompt_command(query)
        assert content == description
        assert "task_tool" in dispatch
        assert "statusline-setup" in dispatch
        assert description in dispatch
        assert _STATUSLINE_SETUP_PROMPT not in dispatch

    @pytest.mark.parametrize("subcommand", sorted(_STATUSLINE_KNOWN_SUBCOMMANDS))
    @staticmethod
    def test_known_subcommands_are_not_dispatched(subcommand: str):
        query = f"/statusline {subcommand} value"
        dispatch, content = _handle_statusline_prompt_command(query)
        assert dispatch == ""
        assert content == query

    @pytest.mark.parametrize("query", ["", "hello", "/statusline", "/skills use demo"])
    @staticmethod
    def test_unmatched_input_is_unchanged(query: str):
        dispatch, content = _handle_statusline_prompt_command(query)
        assert dispatch == ""
        assert content == query


class TestStatuslineSetupPrompt:
    @staticmethod
    def test_contains_identity_schema_and_runtime_contract():
        assert "built-in statusline-setup subagent" in _STATUSLINE_SETUP_PROMPT
        assert "~/.jiuwenswarm-tui/config.json" in _STATUSLINE_SETUP_PROMPT
        assert '"type":"command"' in _STATUSLINE_SETUP_PROMPT
        assert "every 2 seconds" in _STATUSLINE_SETUP_PROMPT

    @staticmethod
    def test_preserves_and_reviews_existing_configuration():
        assert "Always read the config before acting" in _STATUSLINE_SETUP_PROMPT
        assert "Preserve every unrelated field" in _STATUSLINE_SETUP_PROMPT
        assert "remove only the `statusLine` field" in _STATUSLINE_SETUP_PROMPT
        assert "Do not replace a working status line" in _STATUSLINE_SETUP_PROMPT

    @staticmethod
    def test_contains_platform_specific_setup():
        assert "statusline.ps1" in _STATUSLINE_SETUP_PROMPT
        assert "ConvertFrom-Json" in _STATUSLINE_SETUP_PROMPT
        assert "statusline.sh" in _STATUSLINE_SETUP_PROMPT
        assert "jq" in _STATUSLINE_SETUP_PROMPT

    @staticmethod
    def test_contains_required_json_fields_and_safety_rules():
        assert "usage.total_tokens" in _STATUSLINE_SETUP_PROMPT
        assert "context_window.used_percentage" in _STATUSLINE_SETUP_PROMPT
        assert "Never put secrets" in _STATUSLINE_SETUP_PROMPT


class TestBuildUserPromptStatusline:
    @pytest.mark.parametrize(
        ("language", "description"),
        [("zh", "显示模型和上下文"), ("en", "show model and context")],
    )
    @staticmethod
    def test_statusline_rewrites_parent_turn_to_subagent_dispatch(
        language: str,
        description: str,
    ):
        prompt = build_user_prompt(
            f"/statusline {description}",
            files={},
            channel="tui",
            language=language,
        )
        envelope = _extract_envelope(prompt)
        content = envelope["content"]
        assert "task_tool" in content
        assert "statusline-setup" in content
        assert description in content
        assert "/statusline" not in content
        assert _STATUSLINE_SETUP_PROMPT not in prompt

    @staticmethod
    def test_statusline_keeps_envelope_metadata():
        prompt = build_user_prompt(
            "/statusline show git branch",
            files={"test.py": "content"},
            channel="tui",
            language="en",
            trusted_dirs=["/home/user/project"],
        )
        envelope = _extract_envelope(prompt)
        assert envelope["source"] == "tui"
        assert envelope["timezone"] == "Asia/Shanghai"
        assert envelope["type"] == "user input"
        assert envelope["trusted_dirs"] == json.dumps(["/home/user/project"])

    @pytest.mark.parametrize(
        "message",
        ["normal message", "/skills use demo, hello", "/statusline set 'echo ok'"],
    )
    @staticmethod
    def test_other_messages_are_not_rewritten(message: str):
        prompt = build_user_prompt(
            message,
            files={},
            channel="tui",
            language="en",
        )
        envelope = _extract_envelope(prompt)
        assert envelope["content"] == message
        assert "statusline-setup" not in prompt

    @staticmethod
    def test_dispatch_does_not_use_skill_loading():
        prompt = build_user_prompt(
            "/statusline show model",
            files={},
            channel="tui",
            language="en",
        )
        assert "[Skill:" not in prompt
        assert "SkillUseRail" not in prompt
        assert "SKILL.md" not in prompt


class TestStatuslineKnownSubcommands:
    @staticmethod
    def test_expected_subcommands():
        assert _STATUSLINE_KNOWN_SUBCOMMANDS == {
            "set",
            "padding",
            "clear",
            "help",
            "json",
            "get",
        }
