# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from jiuwenswarm.channels.process_cli import duplex_protocol
from jiuwenswarm.channels.process_cli.duplex_protocol import (
    DuplexControl,
    DuplexProtocolError,
    decode_control,
)
from jiuwenswarm.channels.process_cli.protocol.version import (
    CURRENT_SCHEMA_VERSION,
    require_supported_schema_version,
)


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _answer(**overrides: Any) -> dict[str, Any]:
    record = {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "type": "answer",
        "request_id": "request-1",
        "session_id": "session-1",
        "interaction_id": "interaction-1",
        "answers": [{"id": "question-1", "value": "你好"}],
    }
    record.update(overrides)
    return record


def _cancel(**overrides: Any) -> dict[str, Any]:
    record = {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "type": "cancel",
        "request_id": "request-1",
    }
    record.update(overrides)
    return record


def _encode(record: Any) -> bytes:
    return json.dumps(record, ensure_ascii=False).encode("utf-8")


def test_answer_round_trip_preserves_opaque_answer_values() -> None:
    record = _answer(
        answers=[
            {"id": "question-1", "value": "你好"},
            {"values": [1, -2.5, True, None, {"nested": ["choice"]}]},
            {},
        ]
    )

    control = decode_control(_encode(record))

    assert control.kind == "answer"
    assert control.request_id == "request-1"
    assert control.session_id == "session-1"
    assert control.interaction_id == "interaction-1"
    assert isinstance(control.answers, tuple)
    assert all(isinstance(answer, dict) for answer in control.answers)
    assert control.to_dict() == record
    assert decode_control(_encode(control.to_dict())) == control


@pytest.mark.parametrize(
    "extra", [{}, {"session_id": None}, {"session_id": "session-1"}]
)
def test_cancel_accepts_optional_session_and_has_no_answer_fields(
    extra: dict[str, Any],
) -> None:
    control = decode_control(_encode(_cancel(**extra)))

    assert control.kind == "cancel"
    assert control.request_id == "request-1"
    assert control.session_id == extra.get("session_id")
    assert control.interaction_id is None
    assert control.answers == ()
    assert set(control.to_dict()) == {
        "schema_version",
        "type",
        "request_id",
        "session_id",
    }
    assert decode_control(_encode(control.to_dict())) == control


def test_identity_fields_use_existing_schema_whitespace_normalization() -> None:
    record = _answer(
        request_id=" request-1 ",
        session_id=" session-1 ",
        interaction_id=" interaction-1 ",
        answers=[{"value": "  preserve answer whitespace  "}],
    )

    control = decode_control(_encode(record))

    assert control.request_id == "request-1"
    assert control.session_id == "session-1"
    assert control.interaction_id == "interaction-1"
    assert control.answers[0]["value"] == "  preserve answer whitespace  "


def test_decode_uses_the_shared_schema_version_validator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    versions: list[object] = []

    def validate(version: object) -> str:
        versions.append(version)
        return require_supported_schema_version(version)

    monkeypatch.setattr(duplex_protocol, "require_supported_schema_version", validate)

    decode_control(_encode(_answer()))
    decode_control(_encode(_cancel()))

    assert versions == [CURRENT_SCHEMA_VERSION, CURRENT_SCHEMA_VERSION]


@pytest.mark.parametrize("version", ["future-secret", "0.2", None, 0.1, True, {}])
def test_wrong_schema_versions_are_safe_control_errors(version: Any) -> None:
    with pytest.raises(DuplexProtocolError, match="schema_version") as caught:
        decode_control(_encode(_answer(schema_version=version)))

    assert caught.value.code == "INVALID_CONTROL"
    assert caught.value.request_id == "request-1"
    assert "future-secret" not in str(caught.value)


@pytest.mark.parametrize("field", list(_answer()))
def test_answer_requires_every_wire_field(field: str) -> None:
    record = _answer()
    del record[field]

    with pytest.raises(DuplexProtocolError):
        decode_control(_encode(record))


@pytest.mark.parametrize("field", ["schema_version", "type", "request_id"])
def test_cancel_requires_schema_type_and_request_id(field: str) -> None:
    record = _cancel()
    del record[field]

    with pytest.raises(DuplexProtocolError):
        decode_control(_encode(record))


@pytest.mark.parametrize("field", ["request_id", "session_id", "interaction_id"])
@pytest.mark.parametrize("value", [None, "", " \n\t", 12, False, [], {}])
def test_answer_identity_fields_must_be_nonempty_strings(
    field: str, value: Any
) -> None:
    with pytest.raises(DuplexProtocolError, match=field) as caught:
        decode_control(_encode(_answer(**{field: value})))

    assert caught.value.code == "INVALID_CONTROL"
    expected_id = None if field == "request_id" else "request-1"
    assert caught.value.request_id == expected_id


@pytest.mark.parametrize("value", ["", " ", False, 12, [], {}])
def test_cancel_optional_session_must_be_valid_when_present(value: Any) -> None:
    with pytest.raises(DuplexProtocolError, match="session_id"):
        decode_control(_encode(_cancel(session_id=value)))


@pytest.mark.parametrize(
    "answers", [None, [], {}, "secret-answer", [None], ["secret-answer"], [1]]
)
def test_answer_requires_a_nonempty_array_of_objects(answers: Any) -> None:
    with pytest.raises(DuplexProtocolError, match="answers") as caught:
        decode_control(_encode(_answer(answers=answers)))

    assert "secret-answer" not in str(caught.value)


@pytest.mark.parametrize(
    "extra",
    [
        {"interaction_id": None},
        {"answers": []},
        {"input": "secret-value"},
        {"source": "secret-value"},
        {"work_mode": "secret-value"},
        {"config": {"api_key": "secret-value"}},
        {"secret-key": "secret-value"},
    ],
)
def test_cancel_rejects_answer_fields_and_host_overrides(extra: dict[str, Any]) -> None:
    with pytest.raises(DuplexProtocolError, match="unknown fields") as caught:
        decode_control(_encode(_cancel(**extra)))

    assert "secret" not in str(caught.value)


@pytest.mark.parametrize(
    "extra",
    [
        {"source": "secret-value"},
        {"work_mode": "secret-value"},
        {"workspace": {"cwd": "secret-value"}},
        {"config": {"api_key": "secret-value"}},
        {"secret-key": "secret-value"},
    ],
)
def test_answer_rejects_unknown_fields_and_host_overrides(
    extra: dict[str, Any],
) -> None:
    with pytest.raises(DuplexProtocolError, match="unknown fields") as caught:
        decode_control(_encode(_answer(**extra)))

    assert "secret" not in str(caught.value)


@pytest.mark.parametrize(
    "kind", ["run", "event", "result", "secret-kind", None, 12, {}]
)
def test_second_run_and_unknown_record_types_are_rejected(kind: Any) -> None:
    with pytest.raises(DuplexProtocolError, match="control type") as caught:
        decode_control(_encode(_answer(type=kind)))

    assert "secret-kind" not in str(caught.value)
    assert caught.value.request_id == "request-1"


@pytest.mark.parametrize(
    "line",
    [b"", b" ", b"{", b"[]", b"null", b"42", b'"secret-value"', b"{} {}", b"{}\n{}"],
)
def test_invalid_json_is_mapped_to_safe_control_error(line: bytes) -> None:
    with pytest.raises(DuplexProtocolError) as caught:
        decode_control(line)

    assert caught.value.code == "INVALID_CONTROL"
    assert caught.value.request_id is None
    assert "secret-value" not in str(caught.value)


@pytest.mark.parametrize("line", ["{}", None, b"secret-value\xff"])
def test_decoder_requires_utf8_bytes(line: Any) -> None:
    with pytest.raises(DuplexProtocolError, match="UTF-8") as caught:
        decode_control(line)

    assert "secret-value" not in str(caught.value)


@pytest.mark.parametrize(
    "extra",
    [
        b'"request_id":"request-2"',
        b'"\\u0072equest_id":"request-2"',
        b'"unknown":{"secret-key":1,"secret-key":2}',
    ],
)
def test_duplicate_keys_are_rejected_at_every_depth(extra: bytes) -> None:
    line = _encode(_cancel())[:-1] + b"," + extra + b"}"

    with pytest.raises(DuplexProtocolError, match="duplicate JSON keys") as caught:
        decode_control(line)

    assert caught.value.code == "INVALID_CONTROL"
    assert "secret-key" not in str(caught.value)


@pytest.mark.parametrize("number", [b"NaN", b"Infinity", b"-Infinity", b"1e9999"])
def test_nonfinite_numbers_are_rejected_even_inside_answer_payload(
    number: bytes,
) -> None:
    encoded = _encode(_answer(answers=[{"value": "NUMBER"}]))
    line = encoded.replace(b'"NUMBER"', number)

    with pytest.raises(DuplexProtocolError, match="finite") as caught:
        decode_control(line)

    assert caught.value.code == "INVALID_CONTROL"
    assert caught.value.request_id == "request-1"


def test_constructor_and_to_dict_defensively_copy_nested_answers() -> None:
    answers = ({"value": {"items": ["original"]}},)
    control = DuplexControl(
        kind="answer",
        request_id="request-1",
        session_id="session-1",
        interaction_id="interaction-1",
        answers=answers,
    )
    answers[0]["value"]["items"].append("source-change")
    serialized = control.to_dict()
    serialized["answers"][0]["value"]["items"].append("serialized-change")

    assert control.answers[0]["value"]["items"] == ["original"]
    assert control.to_dict()["answers"][0]["value"]["items"] == ["original"]
    with pytest.raises(FrozenInstanceError):
        control.kind = "cancel"


@pytest.mark.parametrize(
    "overrides",
    [
        {"kind": "run"},
        {"request_id": ""},
        {"session_id": None},
        {"interaction_id": None},
        {"answers": ()},
        {"answers": ({1: "secret-value"},)},
        {"answers": ({"value": object()},)},
        {"answers": ({"value": float("nan")},)},
    ],
)
def test_public_constructor_validates_the_same_contract(
    overrides: dict[str, Any],
) -> None:
    values = {
        "kind": "answer",
        "request_id": "request-1",
        "session_id": "session-1",
        "interaction_id": "interaction-1",
        "answers": ({"value": "answer"},),
    }
    values.update(overrides)

    with pytest.raises(DuplexProtocolError) as caught:
        DuplexControl(**values)

    assert "secret-value" not in str(caught.value)


@pytest.mark.parametrize(
    "overrides", [{"interaction_id": "interaction-1"}, {"answers": ({},)}]
)
def test_cancel_constructor_rejects_answer_fields(overrides: dict[str, Any]) -> None:
    with pytest.raises(DuplexProtocolError, match="answer fields"):
        DuplexControl(kind="cancel", request_id="request-1", **overrides)


def test_constructor_rejects_cyclic_answer_values_safely() -> None:
    answer: dict[str, Any] = {}
    answer["cycle"] = answer

    with pytest.raises(DuplexProtocolError, match="nesting"):
        DuplexControl(
            kind="answer",
            request_id="request-1",
            session_id="session-1",
            interaction_id="interaction-1",
            answers=(answer,),
        )


def test_cold_import_is_transport_only_and_does_not_change_protocol_exports() -> None:
    script = """
import sys
import jiuwenswarm.channels.process_cli.duplex_protocol
import jiuwenswarm.channels.process_cli.protocol as protocol

for name in sys.modules:
    assert not name.startswith('jiuwenswarm.runtime'), name
    assert name != 'jiuwenswarm.channels.process_cli.client', name
    assert name != 'jiuwenswarm.channels.process_cli.machine', name
assert not hasattr(protocol, 'DuplexControl')
print('TRANSPORT_ONLY')
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "TRANSPORT_ONLY"
