# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from collections.abc import Iterator

import pytest

from jiuwenswarm.channels.process_cli.protocol import (
    OneShotEvent,
    OneShotRunResult,
    RunStatus,
    decode_jsonl,
    encode_jsonl,
    encode_jsonl_record,
    validate_one_shot_records,
)


def _records() -> tuple[OneShotEvent, OneShotRunResult]:
    return (
        OneShotEvent(
            sequence=0,
            request_id="request-1",
            session_id="session-1",
            event_type="chat.delta",
            payload={"event_type": "chat.delta", "delta": "你好"},
        ),
        OneShotRunResult(
            sequence=1,
            request_id="request-1",
            session_id="session-1",
            status=RunStatus.COMPLETED.value,
            exit_code=0,
            output="你好",
        ),
    )


def test_jsonl_round_trip_has_events_then_one_final_result() -> None:
    encoded = encode_jsonl(_records())
    decoded = decode_jsonl(encoded)

    assert decoded == _records()
    assert encoded.count("\n") == 2
    assert '"type":"event"' in encoded.splitlines()[0]
    assert '"type":"result"' in encoded.splitlines()[1]


def test_each_record_can_be_encoded_before_the_run_finishes() -> None:
    event, result = _records()

    event_line = encode_jsonl_record(event)
    result_line = encode_jsonl_record(result)

    assert event_line.endswith("\n")
    assert '"type":"event"' in event_line
    assert '"type":"result"' in result_line
    assert decode_jsonl(event_line + result_line) == _records()


def test_single_record_encoder_rejects_unknown_objects() -> None:
    with pytest.raises(TypeError, match="unsupported record type"):
        encode_jsonl_record(object())  # type: ignore[arg-type]


def test_record_iterable_is_consumed_once() -> None:
    iterations = 0

    def records() -> Iterator[OneShotEvent | OneShotRunResult]:
        nonlocal iterations
        iterations += 1
        if iterations > 1:
            raise AssertionError("record iterator was consumed more than once")
        yield from _records()

    assert encode_jsonl(records()).endswith("\n")
    assert iterations == 1


def test_record_stream_rejects_unknown_record_objects() -> None:
    with pytest.raises(TypeError, match="unsupported record type"):
        validate_one_shot_records([object()])  # type: ignore[list-item]


@pytest.mark.parametrize(
    "records,match",
    [
        ((), "terminal result"),
        ((_records()[0],), "exactly one result"),
        ((_records()[1], _records()[0]), "exactly one result"),
        ((_records()[1], _records()[1]), "exactly one result"),
        (
            (
                OneShotEvent(
                    sequence=1,
                    request_id="request-1",
                    event_type="chat.delta",
                    payload={},
                ),
                _records()[1],
            ),
            "sequence",
        ),
        (
            (
                _records()[0],
                OneShotRunResult(
                    sequence=1,
                    request_id="request-2",
                    session_id="session-1",
                    status=RunStatus.COMPLETED.value,
                    exit_code=0,
                ),
            ),
            "request_id",
        ),
        (
            (
                _records()[0],
                OneShotRunResult(
                    sequence=1,
                    request_id="request-1",
                    session_id="session-2",
                    status=RunStatus.COMPLETED.value,
                    exit_code=0,
                ),
            ),
            "session_id",
        ),
    ],
)
def test_record_stream_rejects_missing_duplicate_or_inconsistent_terminal(
    records: tuple[OneShotEvent | OneShotRunResult, ...],
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        validate_one_shot_records(records)


@pytest.mark.parametrize(
    "document,error_type",
    [
        ("", ValueError),
        ("{}\n", ValueError),
        ("[]\n", TypeError),
        ("not-json\n", ValueError),
        ("{}\n\n", ValueError),
    ],
)
def test_jsonl_decoder_rejects_invalid_or_unknown_records(
    document: str,
    error_type: type[Exception],
) -> None:
    with pytest.raises(error_type):
        decode_jsonl(document)
