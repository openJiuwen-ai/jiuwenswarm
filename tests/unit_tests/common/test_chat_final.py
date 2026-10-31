# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the chat.final protocol field helpers."""

import pytest

from jiuwenswarm.common.chat_final import (
    FINAL_MODE_APPEND,
    FINAL_MODE_PATCH_SEGMENT,
    FINAL_MODE_REPLACE_TURN,
    annotate_chat_final,
    ensure_final_mode_inplace,
)


class TestAnnotateChatFinal:
    def test_none_payload_gets_defaults(self):
        assert annotate_chat_final(None) == {
            "final_mode": FINAL_MODE_PATCH_SEGMENT,
            "event_type": "chat.final",
        }

    def test_missing_event_type_gets_annotated(self):
        payload = {"delta": "hello"}
        assert annotate_chat_final(payload) == {
            "delta": "hello",
            "final_mode": FINAL_MODE_PATCH_SEGMENT,
            "event_type": "chat.final",
        }

    def test_existing_explicit_final_mode_is_not_overwritten(self):
        payload = {"event_type": "chat.final", "final_mode": FINAL_MODE_APPEND}
        assert annotate_chat_final(payload, final_mode=FINAL_MODE_REPLACE_TURN) == {
            "event_type": "chat.final",
            "final_mode": FINAL_MODE_APPEND,
        }

    def test_blank_final_mode_is_treated_as_missing(self):
        payload = {"event_type": "chat.final", "final_mode": "   "}
        result = annotate_chat_final(payload, final_mode=FINAL_MODE_REPLACE_TURN)
        assert result["final_mode"] == FINAL_MODE_REPLACE_TURN

    def test_non_string_final_mode_is_replaced(self):
        payload = {"event_type": "chat.final", "final_mode": 42}
        result = annotate_chat_final(payload, final_mode=FINAL_MODE_APPEND)
        assert result["final_mode"] == FINAL_MODE_APPEND

    def test_custom_final_mode_is_used(self):
        result = annotate_chat_final({"event_type": "chat.final"}, final_mode=FINAL_MODE_APPEND)
        assert result["final_mode"] == FINAL_MODE_APPEND

    @pytest.mark.parametrize("event_type", ["chat.tool_result", "chat.delta", "team.task"])
    def test_non_final_events_pass_through_untouched(self, event_type: str):
        payload = {"event_type": event_type, "data": "x", "final_mode": "append"}
        assert annotate_chat_final(payload) == payload

    def test_non_final_event_without_final_mode_is_not_annotated(self):
        payload = {"event_type": "chat.tool_result"}
        result = annotate_chat_final(payload)
        assert "final_mode" not in result
        assert result["event_type"] == "chat.tool_result"

    def test_input_payload_is_not_mutated(self):
        payload = {"event_type": "chat.final", "delta": "x"}
        annotate_chat_final(payload)
        assert "final_mode" not in payload

    def test_event_type_whitespace_is_stripped_before_check(self):
        payload = {"event_type": "  chat.final  ", "delta": "x"}
        result = annotate_chat_final(payload)
        assert result["final_mode"] == FINAL_MODE_PATCH_SEGMENT
        assert result["event_type"] == "  chat.final  "


class TestEnsureFinalModeInplace:
    def test_sets_missing_final_mode(self):
        payload: dict = {"event_type": "chat.final"}
        ensure_final_mode_inplace(payload)
        assert payload["final_mode"] == FINAL_MODE_PATCH_SEGMENT

    def test_works_without_event_type(self):
        payload: dict = {"delta": "x"}
        ensure_final_mode_inplace(payload, final_mode=FINAL_MODE_APPEND)
        assert payload["final_mode"] == FINAL_MODE_APPEND
        assert "event_type" not in payload

    def test_existing_explicit_final_mode_is_kept(self):
        payload: dict = {"event_type": "chat.final", "final_mode": FINAL_MODE_REPLACE_TURN}
        ensure_final_mode_inplace(payload)
        assert payload["final_mode"] == FINAL_MODE_REPLACE_TURN

    def test_blank_final_mode_is_overwritten(self):
        payload: dict = {"event_type": "chat.final", "final_mode": ""}
        ensure_final_mode_inplace(payload)
        assert payload["final_mode"] == FINAL_MODE_PATCH_SEGMENT

    def test_non_final_event_type_is_left_alone(self):
        payload: dict = {"event_type": "chat.delta", "final_mode": "append"}
        ensure_final_mode_inplace(payload)
        assert payload == {"event_type": "chat.delta", "final_mode": "append"}

    def test_returns_none_and_mutates_in_place(self):
        payload: dict = {"event_type": "chat.final"}
        assert ensure_final_mode_inplace(payload) is None
        assert "final_mode" in payload
