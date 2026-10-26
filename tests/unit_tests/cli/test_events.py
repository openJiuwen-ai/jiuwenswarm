# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for jiuwenswarm.cli.events."""

from __future__ import annotations

from jiuwenswarm.cli.events import (
    event_kind,
    is_terminal_event,
    needs_user_input,
)


class TestEvents:
    @staticmethod
    def test_event_kind_delta():
        assert event_kind("chat.delta") == "delta"

    @staticmethod
    def test_event_kind_reasoning():
        assert event_kind("chat.reasoning") == "reasoning"

    @staticmethod
    def test_event_kind_tool_call():
        assert event_kind("chat.tool_call") == "tool_call"

    @staticmethod
    def test_event_kind_tool_result():
        assert event_kind("chat.tool_result") == "tool_result"

    @staticmethod
    def test_event_kind_final():
        assert event_kind("chat.final") == "final"

    @staticmethod
    def test_event_kind_error():
        assert event_kind("chat.error") == "error"

    @staticmethod
    def test_event_kind_interactive():
        assert event_kind("chat.ask_user_question") == "interactive"
        assert event_kind("plan.approval_required") == "interactive"

    @staticmethod
    def test_event_kind_processing_status():
        assert event_kind("chat.processing_status") == "processing_status"

    @staticmethod
    def test_event_kind_other():
        assert event_kind("chat.unknown") == "chat"
        assert event_kind("some.other") == "other"

    @staticmethod
    def test_is_terminal_chat_final():
        assert is_terminal_event("chat.final", {}) is True

    @staticmethod
    def test_is_terminal_chat_final_keepalive():
        assert is_terminal_event("chat.final", {"event_type": "keepalive"}) is False

    @staticmethod
    def test_is_terminal_chat_final_llm_usage():
        assert is_terminal_event(
            "chat.final", {"event_type": "chat.llm_usage"}
        ) is False

    @staticmethod
    def test_is_terminal_chat_final_team_runtime_ready():
        # team.runtime_ready is a control event wrapped in chat.final envelope;
        # it must NOT terminate the CLI stream (the team hasn't answered yet).
        assert is_terminal_event(
            "chat.final", {"event_type": "team.runtime_ready"}
        ) is False

    @staticmethod
    def test_is_terminal_chat_final_team_completed():
        # team.completed is a control event; the real terminal signal is
        # chat.processing_status(is_processing=False).
        assert is_terminal_event(
            "chat.final", {"event_type": "team.completed"}
        ) is False

    @staticmethod
    def test_is_terminal_chat_final_team_error():
        # team.error indicates a failed team stream and should terminate.
        assert is_terminal_event(
            "chat.final", {"event_type": "team.error", "error": "boom"}
        ) is True

    @staticmethod
    def test_is_terminal_chat_error():
        assert is_terminal_event("chat.error", {}) is True

    @staticmethod
    def test_is_terminal_processing_done():
        assert is_terminal_event("chat.processing_status", {"is_processing": False}) is True

    @staticmethod
    def test_is_terminal_processing_still_active():
        assert is_terminal_event("chat.processing_status", {"is_processing": True}) is False

    @staticmethod
    def test_needs_user_input_ask():
        assert needs_user_input("chat.ask_user_question") is True

    @staticmethod
    def test_needs_user_input_plan():
        assert needs_user_input("plan.approval_required") is True

    @staticmethod
    def test_needs_user_input_other():
        assert needs_user_input("chat.delta") is False
