# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from copy import deepcopy

import pytest

from jiuwenswarm.channels.process_cli.machine_result import RunSummary
from jiuwenswarm.runtime.events import RuntimeEvent


def _event(event_type: str, **payload: object) -> RuntimeEvent:
    return RuntimeEvent(
        request_id="request_1",
        channel_id="process_cli",
        session_id="session_1",
        payload={"event_type": event_type, **payload},
    )


def test_empty_summary_and_non_answer_observations() -> None:
    summary = RunSummary()
    for event in (
        _event("chat.reasoning", content="private reasoning"),
        _event("chat.tool_result", content="tool output"),
        _event("chat.final", content=""),
        RuntimeEvent("request_1", "process_cli", "session_1", None, is_complete=True),
    ):
        summary.observe(event)

    assert summary.output is None
    assert summary.usage == {}
    assert summary.error is None
    assert not hasattr(summary, "events")
    assert not hasattr(summary, "__dict__")


@pytest.mark.parametrize(
    ("deltas", "final", "expected"),
    [
        ([], "hello", "hello"),
        (["hel", "lo"], "", "hello"),
        (["hel", "lo"], "hello", "hello"),
        (["hel"], "hello", "hello"),
        (["hello "], "world", "hello world"),
        (["hello world"], "world", "hello world"),
        (["  hello", "\n"], " world\n", "  hello\n world\n"),
    ],
)
def test_delta_full_final_and_tail_final_assembly(
    deltas: list[str], final: str, expected: str
) -> None:
    summary = RunSummary()
    for delta in deltas:
        summary.observe(_event("chat.delta", content=delta))
    summary.observe(_event("chat.final", content=final))

    assert summary.output == expected


def test_text_aliases_and_latest_nonempty_final() -> None:
    summary = RunSummary()
    summary.observe(_event("chat.delta", delta="hello "))
    summary.observe(_event("chat.final", answer="hello world"))
    summary.observe(_event("chat.final", text="hello everyone"))
    summary.observe(_event("chat.final", content=""))

    assert summary.output == "hello everyone"


def test_incremental_usage_is_fixed_size_and_ignores_invalid_counters() -> None:
    summary = RunSummary()
    for usage in (
        {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12, "input_cost": 0.1},
        {
            "input_tokens": 20,
            "output_tokens": 3,
            "total_tokens": 23,
            "input_cost": 0.2,
            "cache_tokens": 5,
            "unrecognized_counter": 123,
        },
        {
            "input_tokens": True,
            "output_tokens": "invalid",
            "total_tokens": -1,
            "input_cost": float("nan"),
            "output_cost": float("inf"),
        },
    ):
        summary.observe(
            _event("chat.usage_metadata", metadata={"usage_metadata": usage})
        )

    assert summary.usage == {
        "input_tokens": 30,
        "output_tokens": 5,
        "total_tokens": 35,
        "input_cost": pytest.approx(0.3),
        "cache_tokens": 5,
    }


def test_usage_summary_overrides_deltas_without_double_counting() -> None:
    summary = RunSummary()
    incremental = _event(
        "chat.usage_metadata", metadata={"usage_metadata": {"total_tokens": 12}}
    )
    summary.observe(incremental)
    event = _event(
        "chat.usage_summary",
        usage={"total_tokens": 100, "cache_hit_rate": "75%"},
        context_window_tokens=100000,
        usage_percent=42,
    )
    summary.observe(event)
    summary.observe(event)
    summary.observe(incremental)
    summary.observe(_event("chat.context_usage", total_tokens=90000))

    assert summary.usage == {"total_tokens": 100, "cache_hit_rate": "75%"}

    summary.observe(_event("chat.usage_summary", usage={"total_tokens": 105}))
    assert summary.usage == {"total_tokens": 105}


def test_invalid_usage_payloads_do_not_erase_existing_counters() -> None:
    summary = RunSummary()
    summary.observe(
        _event("chat.usage_metadata", metadata={"usage_metadata": {"total_tokens": 7}})
    )
    for event in (
        _event("chat.usage_summary", usage=None),
        _event("chat.usage_metadata", metadata="invalid"),
        _event("chat.usage_metadata", metadata={"usage_metadata": []}),
    ):
        summary.observe(event)

    assert summary.usage == {"total_tokens": 7}


def test_usage_is_detached_from_event_and_returned_snapshot() -> None:
    summary = RunSummary()
    event = _event("chat.usage_summary", usage={"nested": {"total_tokens": 5}})
    original = deepcopy(event.payload)
    summary.observe(event)
    assert event.payload == original
    assert event.payload is not None
    event.payload["usage"]["nested"]["total_tokens"] = 10
    snapshot = summary.usage
    snapshot["nested"]["total_tokens"] = 20

    assert summary.usage == {"nested": {"total_tokens": 5}}


@pytest.mark.parametrize("event_type", ["chat.error", "runtime.error"])
def test_late_error_marks_run_failed_after_final(event_type: str) -> None:
    summary = RunSummary()
    summary.observe(_event("chat.final", content="partial answer"))
    summary.observe(_event(event_type, error="model connection failed"))
    summary.observe(_event("chat.final", content="later answer"))

    assert summary.output == "later answer"
    assert summary.error is not None
    assert summary.error.code == "RUNTIME_ERROR"
    assert summary.error.message == "model connection failed"


def test_false_ok_is_run_failure_but_local_tool_payload_errors_are_not() -> None:
    summary = RunSummary()
    summary.observe(_event("chat.tool_result", error="command failed", success=False))
    assert summary.error is None
    failure = _event("runtime.observation", message="execution rejected")
    failure.ok = False
    summary.observe(failure)

    assert summary.error is not None
    assert summary.error.message == "execution rejected"


def test_first_failure_and_structured_error_fields_are_preserved() -> None:
    summary = RunSummary()
    event = _event(
        "chat.error",
        error={
            "code": "MODEL_UNAVAILABLE",
            "message": "temporarily unavailable",
            "retryable": True,
            "details": {"provider": "configured"},
        },
        code="OTHER_ERROR",
    )
    event.metadata = {"code": "METADATA_ERROR"}
    before = deepcopy(event)
    summary.observe(event)
    summary.observe(_event("runtime.error", error="secondary failure"))

    assert event == before
    assert summary.error is not None
    assert summary.error.to_dict() == {
        "code": "MODEL_UNAVAILABLE",
        "message": "temporarily unavailable",
        "retryable": True,
        "details": {"provider": "configured"},
    }


def test_runtime_error_metadata_code_and_details_without_transport_leakage() -> None:
    summary = RunSummary()
    event = RuntimeEvent.error(
        request_id="request_1",
        channel_id="process_cli",
        session_id="session_1",
        error=RuntimeError("busy"),
        metadata={
            "code": "SESSION_BUSY",
            "retryable": True,
            "details": {"scope": "session"},
            "transport_secret": "must not be copied",
        },
    )
    summary.observe(event)

    assert summary.error is not None
    assert summary.error.to_dict() == {
        "code": "SESSION_BUSY",
        "message": "busy",
        "retryable": True,
        "details": {"scope": "session"},
    }


def test_error_code_alias_and_malformed_optional_diagnostics() -> None:
    summary = RunSummary()
    summary.observe(
        _event(
            "chat.error",
            error_code=" MODEL_FAILED ",
            error={"message": "failed", "details": {"object": object()}},
            retryable="yes",
        )
    )

    assert summary.error is not None
    assert summary.error.to_dict() == {
        "code": "MODEL_FAILED",
        "message": "failed",
        "retryable": False,
        "details": {},
    }


def test_payloadless_failure_has_stable_fallback() -> None:
    summary = RunSummary()
    summary.observe(RuntimeEvent("request_1", "process_cli", None, None, ok=False))

    assert summary.error is not None
    assert summary.error.code == "RUNTIME_ERROR"
    assert summary.error.message == "Runtime execution failed"
