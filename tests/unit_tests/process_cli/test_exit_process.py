# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Real OS subprocess/pipes with fault-injected clients, not model E2E."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
from pathlib import Path

import pytest

from jiuwenswarm.channels.process_cli.protocol import decode_jsonl

ROOT = Path(__file__).resolve().parents[3]
WORKER = Path(__file__).parent / "fixtures" / "exit_worker.py"


def document(**extra) -> bytes:
    return (
        json.dumps(
            {
                "schema_version": "0.1",
                "type": "run",
                "input": "hello",
                "request_id": "exit-process",
                **extra,
            }
        )
        + "\n"
    ).encode()


async def start(scenario: str, *, duplex: bool = True, self_signal: bool = False):
    env = os.environ.copy()
    env.update(PYTHONUTF8="1", PYTHONPATH=str(ROOT), EXIT_TEST_SCENARIO=scenario)
    if self_signal:
        env["EXIT_TEST_SELF_SIGNAL"] = "1"
    return await asyncio.create_subprocess_exec(
        sys.executable,
        str(WORKER),
        "--run-jsonl" if duplex else "--run-json",
        cwd=ROOT,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def dispose(process) -> None:
    if process.returncode is None:
        process.kill()
    await process.wait()
    if process.stdin is not None:
        process.stdin.close()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            await process.stdin.wait_closed()


def check_result(stdout: bytes, stderr: bytes, code: int, error: str | None) -> None:
    records = decode_jsonl(stdout.decode("utf-8"))
    final = records[-1]
    assert final.exit_code == code, stderr.decode("utf-8")
    assert (final.error.code if final.error else None) == error
    assert "Traceback" not in stderr.decode("utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize("duplex", [False, True])
@pytest.mark.parametrize(
    "scenario,code,error",
    [
        ("normal", 0, None),
        ("runtime_failure", 1, "MODEL_FAILED"),
        ("startup_failure", 1, "RUNTIME_FAILED"),
        ("stream_exit", 1, "RUNTIME_FAILED"),
        ("cleanup_failure", 1, "SHUTDOWN_FAILED"),
        ("shutdown_failure", 1, "SHUTDOWN_FAILED"),
        ("timeout", 124, "TIMEOUT"),
    ],
)
async def test_real_process_terminal_agrees_with_exit(scenario, code, error, duplex):
    process = await start(scenario, duplex=duplex)
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(document(timeout_seconds=0.2)), timeout=15
        )
        assert process.returncode == code, stderr.decode()
        check_result(stdout, stderr, code, error)
        assert b"EXIT_TEST:closing" in stderr
        if scenario != "cleanup_failure":
            assert b"EXIT_TEST:runtime-closed" in stderr
    finally:
        await dispose(process)


@pytest.mark.asyncio
@pytest.mark.parametrize("duplex", [False, True])
async def test_signal_before_input_does_not_require_eof(duplex: bool):
    # signal.raise_signal is dispatched inside the real subprocess, including
    # Windows where terminate() is a force kill, not a graceful SIGTERM.
    process = await start("normal", duplex=duplex, self_signal=True)
    try:
        stdout_task = asyncio.create_task(process.stdout.read())
        stderr_task = asyncio.create_task(process.stderr.read())
        await asyncio.wait_for(process.wait(), timeout=10)
        stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
        assert process.returncode == 130
        check_result(stdout, stderr, 130, "CANCELLED")
        assert b"EXIT_TEST:start" not in stderr
    finally:
        await dispose(process)


@pytest.mark.asyncio
async def test_complete_document_without_final_newline_is_accepted():
    process = await start("normal", duplex=False)
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(document().rstrip(b"\n")), timeout=15
        )
        assert process.returncode == 0
        check_result(stdout, stderr, 0, None)
    finally:
        await dispose(process)


@pytest.mark.asyncio
async def test_duplex_half_close_before_interaction_never_approves():
    process = await start("interaction_eof")
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(document()), timeout=15
        )
        assert process.returncode == 130
        check_result(stdout, stderr, 130, "INPUT_CLOSED")
        assert b"EXIT_TEST:cancel" in stderr
        assert b"EXIT_TEST:runtime-closed" in stderr
    finally:
        await dispose(process)


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.name == "nt", reason="Windows terminate is a force kill, not SIGTERM"
)
@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
async def test_parent_signal_during_execution_and_again_during_close(signum):
    process = await start("signal")
    try:
        process.stdin.write(document())
        await process.stdin.drain()
        while b"EXIT_TEST:stream" not in await asyncio.wait_for(
            process.stderr.readline(), 10
        ):
            pass
        process.send_signal(signum)
        while b"EXIT_TEST:closing" not in await asyncio.wait_for(
            process.stderr.readline(), 10
        ):
            pass
        process.send_signal(signum)
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
        assert process.returncode == 130
        check_result(stdout, stderr, 130, "CANCELLED")
        assert b"EXIT_TEST:runtime-closed" in stderr
    finally:
        await dispose(process)
