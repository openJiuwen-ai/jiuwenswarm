# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Entry integration with real pipes and an isolated fake client, not a model."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from jiuwenswarm.channels.process_cli import machine_entry
from jiuwenswarm.channels.process_cli import main as cli_main
from jiuwenswarm.channels.process_cli.duplex_input import DuplexLineReader
from jiuwenswarm.channels.process_cli.protocol import decode_jsonl


PROJECT_ROOT = Path(__file__).resolve().parents[3]
_GUARDED_MAIN = """
import importlib.abc
import sys

class NoExecutionImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        forbidden = (
            'jiuwenswarm.runtime',
            'jiuwenswarm.channels.process_cli.client',
            'jiuwenswarm.channels.process_cli.machine',
            'jiuwenswarm.channels.process_cli.duplex_control',
            'jiuwenswarm.channels.process_cli.repl',
            'jiuwenswarm.channels.process_cli.app',
        )
        for prefix in forbidden:
            if fullname == prefix or fullname.startswith(prefix + '.'):
                raise AssertionError('UNEXPECTED_EXECUTION_IMPORT: ' + fullname)
        return None

sys.meta_path.insert(0, NoExecutionImports())
from jiuwenswarm.channels.process_cli.main import main
main()
"""

_FAKE_CLIENT_MAIN = """
import asyncio
import os
import sys
import threading
import types

from jiuwenswarm.channels.process_cli.machine_io import OneShotWriter

lifecycle = []

def note(stage):
    lifecycle.append(stage)
    print('FAKE_CLIENT:' + stage, flush=True)

async def residual_task():
    try:
        await asyncio.Event().wait()
    finally:
        note('asyncio-shutdown')

class FakeClient:
    async def start(self):
        note('start')
        os.write(1, b'NATIVE_START_DIAGNOSTIC\\n')
        asyncio.create_task(residual_task())
        await asyncio.sleep(0)

    def resolve_mode_capability(self, requested):
        return types.SimpleNamespace(mode=requested, work_mode='code')

    async def create_or_resume_session(self, *, channel_id, session_id):
        assert channel_id == 'process_cli'
        assert session_id is None
        note('session')
        return 'entry-fake-session'

    async def stream(self, request):
        from jiuwenswarm.runtime.events import RuntimeEvent
        assert request.params['supports_user_interaction'] is True
        try:
            note('stream')
            yield RuntimeEvent(
                request_id=request.request_id, channel_id=request.channel_id,
                session_id=request.session_id,
                payload={'event_type': 'chat.delta', 'delta': 'entry done'},
            )
            await asyncio.sleep(0)
            yield RuntimeEvent(
                request_id=request.request_id, channel_id=request.channel_id,
                session_id=request.session_id,
                payload={'event_type': 'chat.final', 'content': 'entry done'},
                is_complete=True,
            )
        finally:
            note('stream-closed')

    async def cancel(self, request):
        note('cancel')

    async def cleanup_session(self, *, channel_id, session_id):
        assert channel_id == 'process_cli'
        assert session_id == 'entry-fake-session'
        note('session-cleaned')
        return True

    async def close(self):
        note('runtime-closed')

client_module = types.ModuleType('jiuwenswarm.channels.process_cli.client')
client_module.InProcessRuntimeClient = FakeClient
sys.modules[client_module.__name__] = client_module

original_write_result = OneShotWriter.write_result

def checked_write_result(self, result):
    assert lifecycle == [
        'start', 'session', 'stream', 'stream-closed', 'session-cleaned',
        'runtime-closed', 'asyncio-shutdown',
    ], lifecycle
    assert threading.enumerate() == [threading.main_thread()]
    lifecycle.append('result-written')
    original_write_result(self, result)

OneShotWriter.write_result = checked_write_result
from jiuwenswarm.channels.process_cli.main import main
main()
"""


def _document(**overrides: Any) -> bytes:
    record = {
        "schema_version": "0.1",
        "type": "run",
        "request_id": "entry-request",
        "input": "entry question",
    }
    record.update(overrides)
    return json.dumps(record).encode("utf-8")


def _environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    return environment


def _run_guarded(
    arguments: list[str], *, document: bytes = b""
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, "-c", _GUARDED_MAIN, *arguments],
        input=document,
        capture_output=True,
        cwd=PROJECT_ROOT,
        env=_environment(),
        timeout=10,
        check=False,
    )


async def _wait_for_exit(process: subprocess.Popen[bytes]) -> None:
    async with asyncio.timeout(10):
        while process.poll() is None:
            await asyncio.sleep(0.01)


def _finish_child(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.kill()
    if process.stdin is not None:
        with contextlib.suppress(OSError):
            process.stdin.close()
        process.stdin = None
    process.communicate(timeout=5)


def test_run_jsonl_is_an_explicit_stdin_only_flag() -> None:
    parser = cli_main.build_parser()
    defaults = parser.parse_args([])
    parsed = parser.parse_args(["--run-jsonl"])

    assert defaults.run_jsonl is False
    assert defaults.run_json is None
    assert parsed.run_jsonl is True
    assert parsed.run_json is None
    assert parsed.prompt is None
    assert "--run-jsonl" in parser.format_help()


def test_run_jsonl_dispatches_to_duplex_entry_without_legacy_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, bool, bool]] = []

    def execute(
        source: str, *, conflicting_arguments: bool = False, json_lines: bool = False
    ) -> int:
        calls.append((source, conflicting_arguments, json_lines))
        return 8

    monkeypatch.setattr(machine_entry, "execute_source", execute)
    monkeypatch.setattr(sys, "argv", ["jiuwenswarm-process", "--run-jsonl"])

    with pytest.raises(SystemExit) as caught:
        cli_main.main()

    assert caught.value.code == 8
    assert calls == [("-", False, True)]


@pytest.mark.parametrize(
    "extra",
    [
        ["prompt"],
        ["--run-json", "-"],
        ["--run-json=unused.json"],
        ["--mode", "code.normal"],
        ["--output", "human"],
        ["--work-mode", "code"],
        ["--session", "existing"],
        ["--cwd", "."],
        ["--_interactive-worker"],
        ["--run-jsonl"],
    ],
)
def test_run_jsonl_rejects_conflicts_before_runtime_import(extra: list[str]) -> None:
    result = _run_guarded(["--run-jsonl", *extra])

    assert result.returncode == 2, result.stderr
    records = decode_jsonl(result.stdout.decode("utf-8"))
    assert len(records) == 1
    assert records[0].error.code == "INVALID_INPUT"
    assert "--run-jsonl" in records[0].error.message
    assert b"UNEXPECTED_EXECUTION_IMPORT" not in result.stderr


def test_run_jsonl_help_exits_without_reading_input_or_loading_runtime() -> None:
    result = _run_guarded(["--run-jsonl", "--help"])

    assert result.returncode == 0, result.stderr
    assert b"--run-jsonl" in result.stdout
    assert result.stderr == b""


@pytest.mark.parametrize(
    "document,request_id",
    [
        (b"", None),
        (b"\n", None),
        (b"{\n", None),
        (b"[]\n", None),
        (b"secret-value\xff\n", None),
        (_document(), None),
        (_document(input="") + b"\n", "entry-request"),
        (_document(schema_version="secret-version") + b"\n", "entry-request"),
        (_document(type="answer") + b"\n", "entry-request"),
        (_document(secret_key="secret-value") + b"\n", "entry-request"),
        (_document()[:-1] + b',"input":"secret-value"}\n', "entry-request"),
    ],
)
def test_invalid_first_line_has_one_result_without_runtime_initialization(
    document: bytes, request_id: str | None
) -> None:
    result = _run_guarded(["--run-jsonl"], document=document)

    assert result.returncode == 2, result.stderr
    records = decode_jsonl(result.stdout.decode("utf-8"))
    assert len(records) == 1
    final = records[0]
    assert final.status == "failed"
    assert final.exit_code == 2
    assert final.error.code == "INVALID_INPUT"
    assert final.sequence == 0
    assert final.session_id is None
    assert final.request_id
    if request_id is not None:
        assert final.request_id == request_id
    assert b"secret-" not in result.stdout + result.stderr
    assert b"UNEXPECTED_EXECUTION_IMPORT" not in result.stdout + result.stderr
    assert b"Traceback" not in result.stderr


@pytest.mark.asyncio
async def test_first_line_starts_before_eof_and_result_follows_cleanup_with_stdin_open() -> (
    None
):
    process = subprocess.Popen(
        [sys.executable, "-c", _FAKE_CLIENT_MAIN, "--run-jsonl"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=PROJECT_ROOT,
        env=_environment(),
    )
    try:
        assert process.stdin is not None
        assert process.stdout is not None
        process.stdin.write(_document() + b"\n")
        process.stdin.flush()
        async with DuplexLineReader(process.stdout) as output_reader:
            first = await asyncio.wait_for(output_reader.read_line(), timeout=10)
            assert first is not None
            first_record = json.loads(first)
            assert first_record["event_type"] == "run.started"
            assert not process.stdin.closed

            await _wait_for_exit(process)

            assert not process.stdin.closed
            lines = [first]
            while True:
                line = await asyncio.wait_for(output_reader.read_line(), timeout=1)
                if line is None:
                    break
                lines.append(line)
        process.stdin.close()
        process.stdin = None
        _, stderr = process.communicate(timeout=5)

        assert process.returncode == 0, stderr.decode("utf-8", errors="replace")
        records = decode_jsonl(b"\n".join(lines).decode("utf-8"))
        assert [record.to_dict()["type"] for record in records] == [
            "event",
            "event",
            "event",
            "result",
        ]
        final = records[-1]
        assert final.status == "completed"
        assert final.exit_code == 0
        assert final.request_id == "entry-request"
        assert final.session_id == "entry-fake-session"
        assert final.output == "entry done"
        assert final.sequence == 3
        assert b"FAKE_CLIENT:runtime-closed" in stderr
        assert b"FAKE_CLIENT:asyncio-shutdown" in stderr
        assert b"NATIVE_START_DIAGNOSTIC" in stderr
        assert b"FAKE_CLIENT" not in b"\n".join(lines)
    finally:
        _finish_child(process)


@pytest.mark.asyncio
async def test_flag_conflict_exits_even_if_stdin_is_open_and_empty() -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", _GUARDED_MAIN, "--run-jsonl", "--output", "human"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=PROJECT_ROOT,
        env=_environment(),
    )
    try:
        assert process.stdin is not None
        await _wait_for_exit(process)
        assert not process.stdin.closed
        process.stdin.close()
        process.stdin = None
        stdout, stderr = process.communicate(timeout=5)

        assert process.returncode == 2, stderr
        records = decode_jsonl(stdout.decode("utf-8"))
        assert len(records) == 1
        assert records[0].error.code == "INVALID_INPUT"
    finally:
        _finish_child(process)
