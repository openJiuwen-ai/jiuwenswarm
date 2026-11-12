# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Structured error code passthrough on ``chat.error`` frames from stream_utils.

``finish_reason`` / ``error_code`` / ``code`` on an upstream error chunk must be
forwarded as ``code`` so downstream classifiers can map the failure
to a precise error kind instead of falling through to ``unknown`` / OA.05000090.
"""

from __future__ import annotations

from types import SimpleNamespace

from jiuwenswarm.server.utils.stream_utils import (
    build_chat_error_event,
    parse_stream_chunk,
)


# ── build_chat_error_event ──────────────────────────────────────────────

def test_forwards_finish_reason_as_code() -> None:
    ev = build_chat_error_event(
        {
            "output": "已达到最大思考轮数",
            "result_type": "error",
            "finish_reason": "max_iterations",
        },
        "已达到最大思考轮数",
    )
    assert ev == {
        "event_type": "chat.error",
        "error": "已达到最大思考轮数",
        "code": "max_iterations",
    }


def test_ignores_placeholder_finish_reason() -> None:
    ev = build_chat_error_event(
        {"output": "x", "result_type": "error", "finish_reason": "null"},
        "x",
    )
    assert "code" not in ev


def test_explicit_code_wins_over_error_code_and_finish_reason() -> None:
    ev = build_chat_error_event(
        {"output": "x", "code": "A", "error_code": "B", "finish_reason": "C"},
        "x",
    )
    assert ev["code"] == "A"


def test_error_code_wins_over_finish_reason() -> None:
    ev = build_chat_error_event(
        {"output": "x", "error_code": "B", "finish_reason": "C"},
        "x",
    )
    assert ev["code"] == "B"


def test_soft_stop_flag_still_sets_code() -> None:
    ev = build_chat_error_event(
        {
            "output": "soft",
            "result_type": "error",
            "finish_reason": "null",
            "soft_stop_recovery_exhausted": True,
        },
        "soft",
    )
    assert ev["code"] == "soft_stop_recovery_exhausted"


def test_no_code_for_plain_error() -> None:
    ev = build_chat_error_event({"output": "x", "result_type": "error"}, "x")
    assert ev == {"event_type": "chat.error", "error": "x"}


def test_non_dict_source_has_no_code() -> None:
    ev = build_chat_error_event("oops", "oops")
    assert ev == {"event_type": "chat.error", "error": "oops"}


# ── parse_stream_chunk (dict / typed / response) ────────────────────────

def test_dict_error_forwards_finish_reason() -> None:
    parsed = parse_stream_chunk(
        {"output": "已达到最大思考轮数", "result_type": "error", "finish_reason": "max_iterations"}
    )
    assert parsed == {
        "event_type": "chat.error",
        "error": "已达到最大思考轮数",
        "code": "max_iterations",
    }


def test_dict_nested_error_forwards_finish_reason() -> None:
    parsed = parse_stream_chunk(
        {
            "output": {
                "output": "已达上限",
                "result_type": "error",
                "finish_reason": "max_iterations",
            }
        }
    )
    assert parsed == {
        "event_type": "chat.error",
        "error": "已达上限",
        "code": "max_iterations",
    }


def test_typed_answer_error_forwards_finish_reason() -> None:
    chunk = SimpleNamespace(
        type="answer",
        payload={
            "output": "已达上限",
            "result_type": "error",
            "finish_reason": "max_iterations",
        },
    )
    assert parse_stream_chunk(chunk) == {
        "event_type": "chat.error",
        "error": "已达上限",
        "code": "max_iterations",
    }


def test_response_error_forwards_finish_reason() -> None:
    chunk = SimpleNamespace(
        request_id="r1",
        payload={
            "output": "已达上限",
            "result_type": "error",
            "finish_reason": "max_iterations",
        },
    )
    assert parse_stream_chunk(chunk) == {
        "event_type": "chat.error",
        "error": "已达上限",
        "code": "max_iterations",
    }


def test_typed_answer_error_without_code_has_no_code() -> None:
    chunk = SimpleNamespace(
        type="answer",
        payload={"output": "模型未返回有效内容", "result_type": "error"},
    )
    parsed = parse_stream_chunk(chunk)
    assert parsed == {"event_type": "chat.error", "error": "模型未返回有效内容"}
