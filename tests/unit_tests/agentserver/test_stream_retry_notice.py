# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Keep team retry diagnostics out of streamed assistant answers."""

from types import SimpleNamespace

import pytest

from jiuwenswarm.server.utils.stream_utils import parse_stream_chunk


@pytest.mark.parametrize("as_dict", [False, True])
def test_model_retry_becomes_a_notice_without_provider_diagnostics(as_dict):
    payload = {
        "content": "[Retry 2/10] RateLimitError: provider response and quota details",
        "retrying": True,
        "attempt": 2,
        "max_attempts": 10,
        "error_code": 181001,
    }
    chunk = {"type": "llm_output", "payload": payload}
    if not as_dict:
        chunk = SimpleNamespace(**chunk)

    parsed = parse_stream_chunk(chunk)

    assert parsed == {
        "event_type": "chat.notice",
        "notice_type": "model_retry",
        "level": "warning",
        "content": "Model call failed; retrying (2/10).",
        "retrying": True,
        "attempt": 2,
        "max_attempts": 10,
        "error_code": 181001,
    }
    assert payload["content"] not in str(parsed)
    assert payload["retrying"] is True


@pytest.mark.parametrize("retrying", [None, False, "true"])
def test_normal_model_output_is_not_reclassified(retrying):
    chunk = SimpleNamespace(
        type="llm_output",
        payload={
            "content": "An answer about [Retry 1/10] errors", "retrying": retrying,
        },
    )

    assert parse_stream_chunk(chunk) == {
        "event_type": "chat.delta",
        "content": "An answer about [Retry 1/10] errors",
    }


def test_retry_without_counters_still_has_a_short_notice():
    chunk = SimpleNamespace(
        type="llm_output", payload={"retrying": True, "content": ""},
    )

    parsed = parse_stream_chunk(chunk)

    assert parsed["event_type"] == "chat.notice"
    assert parsed["content"] == "Model call failed; retrying."


def test_retry_notice_does_not_stringify_invalid_metadata():
    chunk = SimpleNamespace(
        type="llm_output",
        payload={
            "retrying": True,
            "content": "provider diagnostic",
            "attempt": {"provider": "diagnostic"},
            "max_attempts": True,
            "error_code": "provider diagnostic",
        },
    )

    parsed = parse_stream_chunk(chunk)

    assert parsed["content"] == "Model call failed; retrying."
    assert "diagnostic" not in str(parsed)


def test_retry_and_recovery_keep_answer_text_separate():
    chunks = [
        SimpleNamespace(type="llm_output", payload={"content": "Before. "}),
        SimpleNamespace(type="llm_output", payload={
            "content": "[Retry 1/10] provider diagnostic", "retrying": True,
            "attempt": 1, "max_attempts": 10,
        }),
        SimpleNamespace(type="llm_output", payload={"content": "After."}),
    ]
    parsed = [parse_stream_chunk(chunk) for chunk in chunks]

    assert [event["event_type"] for event in parsed] == [
        "chat.delta", "chat.notice", "chat.delta",
    ]
    answer = "".join(
        event["content"] for event in parsed if event["event_type"] == "chat.delta"
    )
    assert answer == "Before. After."


def test_terminal_model_failure_remains_an_error():
    chunk = SimpleNamespace(
        type="controller_output",
        payload={"type": "task_failed", "data": [{"text": "Model calls exhausted"}]},
    )

    assert parse_stream_chunk(chunk) == {
        "event_type": "chat.error", "error": "Model calls exhausted",
    }


def test_retry_notice_history_roundtrip_keeps_diagnostics_out(tmp_path, monkeypatch):
    from jiuwenswarm.server.runtime.session import session_history

    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)
    notice = parse_stream_chunk(SimpleNamespace(type="llm_output", payload={
        "content": "[Retry 1/10] raw provider response",
        "retrying": True, "attempt": 1, "max_attempts": 10,
    }))
    answer = parse_stream_chunk(SimpleNamespace(type="answer", payload={
        "output": {"output": "Recovered answer"}, "result_type": "answer",
    }))
    for event in (notice, answer):
        session_history.append_history_record(
            session_id="retry-history", request_id="same-round", channel_id="web",
            role="assistant", event_type=event["event_type"],
            content=event["content"], timestamp=1.0,
            extra={key: value for key, value in event.items()
                   if key not in ("event_type", "content")},
        )
    session_history.flush_history_writes()

    records = session_history.load_history_records("retry-history")
    assert [record["event_type"] for record in records] == [
        "chat.notice", "chat.final",
    ]
    assert records[-1]["content"] == "Recovered answer"
    assert "raw provider response" not in str(records)
