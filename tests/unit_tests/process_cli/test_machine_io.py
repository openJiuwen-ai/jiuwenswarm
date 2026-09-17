# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from jiuwenswarm.channels.process_cli import machine_io
from jiuwenswarm.channels.process_cli.machine_io import (
    MachineInputError,
    OneShotWriter,
    read_run_input,
)
from jiuwenswarm.channels.process_cli.protocol import (
    OneShotRunInput,
    OneShotRunResult,
    RuntimeErrorInfo,
    decode_jsonl,
)
from jiuwenswarm.runtime.events import RuntimeEvent


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _run_document(**overrides: Any) -> str:
    value = {
        "schema_version": "0.1",
        "type": "run",
        "request_id": "external-request",
        "input": "hello",
    }
    value.update(overrides)
    return json.dumps(value, ensure_ascii=False)


def _event(*, payload: dict[str, Any] | None = None) -> RuntimeEvent:
    return RuntimeEvent(
        request_id="internal-request",
        channel_id="internal-channel",
        session_id="internal-session",
        payload=payload,
        metadata={"credential": "internal-secret"},
        is_complete=True,
    )


def _result() -> OneShotRunResult:
    return OneShotRunResult(
        sequence=99,
        request_id="result-request",
        session_id="result-session",
        status="completed",
        exit_code=0,
        output="done\nnext line",
        usage={"output_tokens": 3},
    )


class ObservedOutput(io.StringIO):
    def __init__(self, *, fail_at: str | None = None) -> None:
        super().__init__()
        self.write_count = 0
        self.flush_count = 0
        self.fail_at = fail_at

    def write(self, value: str) -> int:
        self.write_count += 1
        if self.fail_at == "write":
            raise BrokenPipeError("consumer closed the pipe")
        if self.fail_at == "short_write":
            return super().write(value[:3])
        return super().write(value)

    def flush(self) -> None:
        self.flush_count += 1
        if self.fail_at == "flush":
            raise OSError("flush failed")
        super().flush()


class ChunkedInput(io.StringIO):
    def __init__(self, document: str) -> None:
        super().__init__(document)
        self.reached_eof = False
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> str:
        self.read_sizes.append(size)
        result = super().read(min(size, 7))
        if not result:
            self.reached_eof = True
        return result


def test_file_input_is_utf8_and_uses_existing_schema(tmp_path: Path) -> None:
    path = tmp_path / "run.json"
    document = _run_document(input="你好\n  保留空白  ")
    path.write_text(document, encoding="utf-8")

    result = read_run_input(str(path))

    assert result == OneShotRunInput.from_dict(json.loads(document))
    assert result.input == "你好\n  保留空白  "


def test_stdin_is_read_to_eof_with_bounded_reads_and_is_not_closed() -> None:
    stream = ChunkedInput(_run_document())

    result = read_run_input("-", stdin=stream)

    assert result.input == "hello"
    assert stream.reached_eof
    assert all(0 < size <= 8192 for size in stream.read_sizes)
    assert not stream.closed


def test_omitted_stdin_uses_current_process_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(_run_document()))

    assert read_run_input("-").request_id == "external-request"


def test_stdin_binary_buffer_avoids_locale_decoding() -> None:
    raw = io.BytesIO(_run_document(input="你好").encode("utf-8"))
    stream = io.TextIOWrapper(raw, encoding="ascii")
    try:
        assert read_run_input("-", stdin=stream).input == "你好"
        assert not stream.closed
    finally:
        stream.close()


@pytest.mark.parametrize("document", ["", " ", "\r\n\t"])
def test_empty_input_is_rejected(document: str) -> None:
    with pytest.raises(MachineInputError, match="empty") as caught:
        read_run_input("-", stdin=io.StringIO(document))

    assert caught.value.code == "INVALID_INPUT"
    assert caught.value.request_id is None


@pytest.mark.parametrize(
    "document",
    [
        "not-json",
        "{",
        "[]",
        "null",
        '"secret-input"',
        "{} {}",
        _run_document() + "\n" + _run_document(),
        '{"input":"secret-input",}',
    ],
)
def test_invalid_json_and_multiple_documents_are_rejected(document: str) -> None:
    with pytest.raises(MachineInputError) as caught:
        read_run_input("-", stdin=io.StringIO(document))

    assert caught.value.code == "INVALID_INPUT"
    assert caught.value.request_id is None
    assert "secret-input" not in str(caught.value)


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"schema_version": "secret-version"}, "schema_version"),
        ({"schema_version": 1}, "schema_version"),
        ({"type": "secret-type"}, "schema"),
        ({"input": " "}, "input"),
        ({"input": 12}, "input"),
        ({"mode": "secret-mode"}, "mode"),
        ({"session_id": []}, "session_id"),
        ({"secret-field": "secret-value"}, "unknown fields"),
        ({"agent": {"name": "agent-one"}}, "missing required fields"),
        ({"agent": {"secret-field": "secret-value"}}, "unknown fields"),
        ({"agent": {"name": "x", "instructions": "safe"}}, "name"),
        ({"workspace": {"secret-field": "secret-value"}}, "unknown fields"),
        ({"workspace": {"trusted_dirs": [12]}}, "trusted_dirs"),
        ({"timeout_seconds": 0}, "timeout_seconds"),
        ({"timeout_seconds": True}, "timeout_seconds"),
        ({"timeout_seconds": 10**400}, "schema"),
    ],
)
def test_schema_rejections_preserve_id_without_echoing_input(
    overrides: dict[str, Any], reason: str
) -> None:
    document = _run_document(**overrides)

    with pytest.raises(MachineInputError, match=reason) as caught:
        read_run_input("-", stdin=io.StringIO(document))

    assert caught.value.code == "INVALID_INPUT"
    assert caught.value.request_id == "external-request"
    assert "secret-" not in str(caught.value)
    assert document not in str(caught.value)


def test_missing_top_level_fields_have_safe_error() -> None:
    with pytest.raises(MachineInputError, match="missing required fields") as caught:
        read_run_input("-", stdin=io.StringIO('{"request_id":"external-request"}'))

    assert caught.value.request_id == "external-request"


@pytest.mark.parametrize("request_id", [None, "", "  ", 12, [], {}])
def test_invalid_request_id_is_not_used_for_error_correlation(request_id: Any) -> None:
    document = _run_document(request_id=request_id, input="")

    with pytest.raises(MachineInputError) as caught:
        read_run_input("-", stdin=io.StringIO(document))

    assert caught.value.request_id is None


def test_error_request_id_uses_the_schema_normalization() -> None:
    document = _run_document(request_id=" external-request ", input="")

    with pytest.raises(MachineInputError) as caught:
        read_run_input("-", stdin=io.StringIO(document))

    assert caught.value.request_id == "external-request"


@pytest.mark.parametrize(
    "extra",
    [
        '"input":"second-input"',
        '"agent":{"name":"agent-one","instructions":"a","instructions":"b"}',
        '"workspace":{"cwd":"a","cwd":"b"}',
    ],
)
def test_duplicate_json_keys_are_rejected_at_every_level(extra: str) -> None:
    document = _run_document()[:-1] + "," + extra + "}"

    with pytest.raises(MachineInputError, match="duplicate JSON keys") as caught:
        read_run_input("-", stdin=io.StringIO(document))

    assert caught.value.request_id == "external-request"


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_json_constants_are_rejected(constant: str) -> None:
    document = _run_document()[:-1] + ',"timeout_seconds":' + constant + "}"

    with pytest.raises(MachineInputError, match="finite JSON numbers") as caught:
        read_run_input("-", stdin=io.StringIO(document))

    assert caught.value.request_id == "external-request"


def test_deeply_nested_json_is_reported_as_invalid_input() -> None:
    document = "[" * 2000 + "0" + "]" * 2000

    with pytest.raises(MachineInputError, match="valid JSON document"):
        read_run_input("-", stdin=io.StringIO(document))


def test_missing_file_does_not_disclose_the_path(tmp_path: Path) -> None:
    with pytest.raises(MachineInputError, match="could not be read") as caught:
        read_run_input(str(tmp_path / "secret-path.json"))

    assert "secret-path" not in str(caught.value)


def test_invalid_utf8_file_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "invalid.json"
    path.write_bytes(b'\xff{"input":"secret-value"}')

    with pytest.raises(MachineInputError, match="UTF-8") as caught:
        read_run_input(str(path))

    assert "secret-value" not in str(caught.value)


def test_closed_stdin_is_reported_as_invalid_input() -> None:
    stream = io.StringIO(_run_document())
    stream.close()

    with pytest.raises(MachineInputError, match="could not be read"):
        read_run_input("-", stdin=stream)


@pytest.mark.parametrize("source", ["file", "stdin"])
def test_limit_is_measured_in_utf8_bytes(
    source: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = _run_document(input="你好")
    encoded_size = len(document.encode("utf-8"))
    monkeypatch.setattr(machine_io, "MAX_RUN_INPUT_BYTES", encoded_size - 1)
    path = tmp_path / "run.json"
    if source == "file":
        path.write_text(document, encoding="utf-8")

    def read() -> OneShotRunInput:
        if source == "stdin":
            return read_run_input("-", stdin=io.StringIO(document))
        return read_run_input(str(path))

    with pytest.raises(MachineInputError, match="byte limit"):
        read()

    monkeypatch.setattr(machine_io, "MAX_RUN_INPUT_BYTES", encoded_size)
    assert read().input == "你好"


def test_events_flush_immediately_and_use_external_identity() -> None:
    output = ObservedOutput()
    writer = OneShotWriter(output, request_id="external-request")
    event = _event(payload={"event_type": "chat.delta", "delta": "你好\nnext"})
    original = event.to_dict()

    assert writer.sequence == 0
    assert writer.session_id is None
    writer.write_event(event)
    first = json.loads(output.getvalue())

    assert output.write_count == 1
    assert output.flush_count == 1
    assert output.getvalue().count("\n") == 1
    assert first["sequence"] == 0
    assert first["request_id"] == "external-request"
    assert first["session_id"] is None
    assert first["payload"] == event.payload
    assert "internal-secret" not in output.getvalue()
    assert event.to_dict() == original
    assert writer.sequence == 1
    assert not writer.final_written

    writer.session_id = "resolved-session"
    writer.write_event(event)
    second = json.loads(output.getvalue().splitlines()[1])

    assert second["sequence"] == 1
    assert second["session_id"] == "resolved-session"
    assert output.flush_count == 2
    assert event.to_dict() == original


def test_none_payload_remains_null_without_inferring_session() -> None:
    output = ObservedOutput()
    writer = OneShotWriter(output, request_id="external-request")
    event = _event()

    writer.write_event(event)

    record = json.loads(output.getvalue())
    assert record["event_type"] == "runtime.event"
    assert record["payload"] is None
    assert record["session_id"] is None
    assert event.payload is None
    assert event.session_id == "internal-session"


def test_result_uses_writer_sequence_identity_and_preserves_original() -> None:
    output = ObservedOutput()
    writer = OneShotWriter(output, request_id="external-request")
    writer.session_id = "resolved-session"
    result = _result()
    original = result.to_dict()
    writer.write_event(_event())

    writer.write_result(result)

    records = decode_jsonl(output.getvalue())
    final = records[-1]
    assert len(records) == 2
    assert final.sequence == 1
    assert final.request_id == "external-request"
    assert final.session_id == "resolved-session"
    assert final.output == "done\nnext line"
    assert final.usage == {"output_tokens": 3}
    assert output.write_count == output.flush_count == 2
    assert writer.sequence == 2
    assert writer.final_written
    assert not writer.broken
    assert result.to_dict() == original


def test_input_error_can_be_the_only_output_record() -> None:
    output = ObservedOutput()
    writer = OneShotWriter(output, request_id="external-request")
    result = OneShotRunResult(
        sequence=9,
        request_id="placeholder-request",
        status="failed",
        exit_code=2,
        error=RuntimeErrorInfo(code="INVALID_INPUT", message="input is invalid"),
    )

    writer.write_result(result)

    final = decode_jsonl(output.getvalue())[0]
    assert final.sequence == 0
    assert final.session_id is None
    assert final.request_id == "external-request"
    assert final.error.code == "INVALID_INPUT"
    assert final.error.message == "input is invalid"
    assert output.flush_count == 1


def test_final_result_is_written_at_most_once_and_forbids_later_events() -> None:
    output = ObservedOutput()
    writer = OneShotWriter(output, request_id="external-request")
    writer.session_id = "resolved-session"
    writer.write_result(_result())

    with pytest.raises(ValueError, match="already been written"):
        writer.write_result(_result())
    with pytest.raises(ValueError, match="already been written"):
        writer.write_event(_event())

    assert output.write_count == output.flush_count == 1
    assert writer.sequence == 1


@pytest.mark.parametrize("fail_at", ["write", "flush", "short_write"])
@pytest.mark.parametrize("record_type", ["event", "result"])
def test_output_failure_marks_broken_and_never_retries(
    fail_at: str, record_type: str
) -> None:
    output = ObservedOutput(fail_at=fail_at)
    writer = OneShotWriter(output, request_id="external-request")
    writer.session_id = "resolved-session"

    with pytest.raises(OSError):
        if record_type == "event":
            writer.write_event(_event())
        else:
            writer.write_result(_result())

    assert writer.broken
    assert writer.sequence == 0
    assert not writer.final_written
    before_retry = (output.write_count, output.flush_count)
    with pytest.raises(BrokenPipeError, match="stream is broken"):
        writer.write_event(_event())
    with pytest.raises(BrokenPipeError, match="stream is broken"):
        writer.write_result(_result())
    assert (output.write_count, output.flush_count) == before_retry


def test_closed_output_marks_writer_broken() -> None:
    output = io.StringIO()
    output.close()
    writer = OneShotWriter(output, request_id="external-request")

    with pytest.raises(ValueError):
        writer.write_event(_event())

    assert writer.broken


def test_writer_rejects_invalid_identity_before_output() -> None:
    with pytest.raises(ValueError, match="empty"):
        OneShotWriter(io.StringIO(), request_id=" ")
    with pytest.raises(TypeError, match="string"):
        OneShotWriter(io.StringIO(), request_id=None)  # type: ignore[arg-type]


def test_cold_import_does_not_load_runtime_or_client() -> None:
    script = """
import sys
import jiuwenswarm.channels.process_cli.machine_io

unexpected = sorted(
    name for name in sys.modules
    if name.startswith('jiuwenswarm.runtime')
    or name == 'jiuwenswarm.channels.process_cli.client'
    or name.startswith('jiuwenswarm.gateway')
)
print('UNEXPECTED=' + repr(unexpected))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "UNEXPECTED=[]" in result.stdout
