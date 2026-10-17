# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
import io
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

import pytest

from jiuwenswarm.channels.process_cli.duplex_input import (
    MAX_DUPLEX_LINE_BYTES,
    DuplexInputError,
    DuplexLineReader,
)


PROJECT_ROOT = Path(__file__).resolve().parents[3]
_OPEN_STDIN_CHILD = """
import asyncio
import sys
import threading

from jiuwenswarm.channels.process_cli.duplex_input import DuplexLineReader

async def main():
    async with DuplexLineReader() as reader:
        first = await reader.read_line()
        assert first == b'{"type":"run"}', first
        pending = asyncio.create_task(reader.read_line())
        await asyncio.sleep(0.02)
        assert not pending.done()
        if sys.argv[1] == 'cancel':
            pending.cancel()
            try:
                await pending
            except asyncio.CancelledError:
                pass
    if sys.argv[1] == 'close':
        assert await pending is None

asyncio.run(main())
assert threading.enumerate() == [threading.main_thread()]
print('EXIT_WITH_STDIN_OPEN', flush=True)
"""


@pytest.fixture
def pipe_streams() -> Iterator[tuple[BinaryIO, BinaryIO]]:
    read_fd, write_fd = os.pipe()
    with os.fdopen(read_fd, "rb", buffering=0) as incoming:
        with os.fdopen(write_fd, "wb", buffering=0) as outgoing:
            yield incoming, outgoing


@pytest.mark.asyncio
@pytest.mark.parametrize("stream_type", [io.BytesIO, io.StringIO])
async def test_memory_stream_yields_each_complete_line_before_eof(stream_type) -> None:
    document = '{"input":"你好"}\r\n{"type":"cancel"}\n'
    content = document.encode("utf-8") if stream_type is io.BytesIO else document
    stream = stream_type(content)

    async with DuplexLineReader(stream) as reader:
        assert await reader.read_line() == '{"input":"你好"}'.encode("utf-8")
        assert await reader.read_line() == b'{"type":"cancel"}'
        assert await reader.read_line() is None
        assert await reader.read_line() is None

    assert reader.closed
    assert not stream.closed


@pytest.mark.asyncio
async def test_default_stream_is_current_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("run\n"))

    async with DuplexLineReader() as reader:
        assert await reader.read_line() == b"run"


@pytest.mark.asyncio
async def test_empty_lines_are_distinct_from_eof() -> None:
    async with DuplexLineReader(io.BytesIO(b"\n\r\n")) as reader:
        assert await reader.read_line() == b""
        assert await reader.read_line() == b""
        assert await reader.read_line() is None


@pytest.mark.asyncio
async def test_pipe_delivers_first_line_while_parent_keeps_input_open(
    pipe_streams: tuple[BinaryIO, BinaryIO],
) -> None:
    incoming, outgoing = pipe_streams
    outgoing.write(b'{"type":"run"}\n')

    async with DuplexLineReader(incoming) as reader:
        first = await asyncio.wait_for(reader.read_line(), timeout=1)
        assert first == b'{"type":"run"}'
        assert not outgoing.closed

        pending = asyncio.create_task(reader.read_line())
        await asyncio.sleep(0.02)
        assert not pending.done()
        outgoing.write(b'{"type":"cancel"}\n')
        assert await asyncio.wait_for(pending, timeout=1) == b'{"type":"cancel"}'


@pytest.mark.asyncio
async def test_pipe_accepts_split_multibyte_utf8_and_split_crlf(
    pipe_streams: tuple[BinaryIO, BinaryIO],
) -> None:
    incoming, outgoing = pipe_streams
    encoded = "你".encode("utf-8")
    async with DuplexLineReader(incoming, max_line_bytes=3) as reader:
        pending = asyncio.create_task(reader.read_line())
        for fragment in (encoded[:1], encoded[1:], b"\r"):
            outgoing.write(fragment)
            await asyncio.sleep(0.02)
            assert not pending.done()
        outgoing.write(b"\n")

        assert await asyncio.wait_for(pending, timeout=1) == encoded


@pytest.mark.asyncio
async def test_pipe_eof_after_full_line_is_clean(
    pipe_streams: tuple[BinaryIO, BinaryIO],
) -> None:
    incoming, outgoing = pipe_streams
    outgoing.write(b"run\n")
    outgoing.close()

    async with DuplexLineReader(incoming) as reader:
        assert await reader.read_line() == b"run"
        assert await asyncio.wait_for(reader.read_line(), timeout=1) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", [b"{}", b"partial\r", b"\xc3"])
async def test_eof_with_partial_line_is_rejected(partial: bytes) -> None:
    reader = DuplexLineReader(io.BytesIO(partial))

    with pytest.raises(DuplexInputError, match="line terminator") as caught:
        await reader.read_line()

    assert caught.value.code == "INVALID_INPUT"
    assert reader.closed
    assert reader.buffered_bytes == 0
    assert await reader.read_line() is None


@pytest.mark.asyncio
async def test_pipe_eof_with_partial_control_line_is_rejected(
    pipe_streams: tuple[BinaryIO, BinaryIO],
) -> None:
    incoming, outgoing = pipe_streams
    outgoing.write(b"run\npartial")
    outgoing.close()

    async with DuplexLineReader(incoming) as reader:
        assert await reader.read_line() == b"run"
        with pytest.raises(DuplexInputError, match="line terminator"):
            await asyncio.wait_for(reader.read_line(), timeout=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [b"secret\xff\n", b"\xc0\xaf\n", b"\xed\xa0\x80\n"])
async def test_invalid_utf8_is_rejected_without_echoing_data(content: bytes) -> None:
    async with DuplexLineReader(io.BytesIO(content)) as reader:
        with pytest.raises(DuplexInputError, match="UTF-8") as caught:
            await reader.read_line()

    assert "secret" not in str(caught.value)
    assert reader.closed


@pytest.mark.asyncio
async def test_stringio_invalid_unicode_is_reported_safely() -> None:
    async with DuplexLineReader(io.StringIO("secret\ud800\n")) as reader:
        with pytest.raises(DuplexInputError, match="UTF-8") as caught:
            await reader.read_line()

    assert "secret" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", [b"\n", b"\r\n"])
async def test_limit_allows_exact_content_size_and_excludes_line_ending(
    ending: bytes,
) -> None:
    content = b"x" * MAX_DUPLEX_LINE_BYTES
    async with DuplexLineReader(io.BytesIO(content + ending)) as reader:
        assert await reader.read_line() == content
        assert await reader.read_line() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", [b"", b"\n", b"\r\n"])
async def test_oversized_line_is_rejected_with_or_without_terminator(
    ending: bytes,
) -> None:
    async with DuplexLineReader(
        io.BytesIO(b"x" * 17 + ending), max_line_bytes=16
    ) as reader:
        with pytest.raises(DuplexInputError, match="byte limit"):
            await reader.read_line()
        assert reader.buffered_bytes == 0


@pytest.mark.asyncio
async def test_line_limit_counts_utf8_bytes_not_characters() -> None:
    async with DuplexLineReader(io.StringIO("你好\n"), max_line_bytes=5) as reader:
        with pytest.raises(DuplexInputError, match="byte limit"):
            await reader.read_line()


@pytest.mark.asyncio
async def test_many_buffered_lines_keep_the_framing_buffer_bounded() -> None:
    async with DuplexLineReader(
        io.BytesIO(b"1234\n" * 1000), max_line_bytes=4
    ) as reader:
        for _ in range(1000):
            assert await reader.read_line() == b"1234"
            assert reader.buffered_bytes <= 6
        assert await reader.read_line() is None


@pytest.mark.asyncio
async def test_close_discards_partial_input_and_wakes_a_long_poll_immediately(
    pipe_streams: tuple[BinaryIO, BinaryIO],
) -> None:
    incoming, outgoing = pipe_streams
    outgoing.write(b"partial")
    reader = DuplexLineReader(incoming, poll_interval=10)
    pending = asyncio.create_task(reader.read_line())
    await asyncio.sleep(0.02)
    assert reader.buffered_bytes == len(b"partial")

    await reader.close()

    assert await asyncio.wait_for(pending, timeout=0.5) is None
    assert reader.closed
    assert reader.buffered_bytes == 0
    assert not incoming.closed
    assert not outgoing.closed
    assert await reader.read_line() is None
    await reader.close()


@pytest.mark.asyncio
async def test_cancelled_read_has_no_executor_or_thread_and_can_resume_partial_line(
    pipe_streams: tuple[BinaryIO, BinaryIO], monkeypatch: pytest.MonkeyPatch
) -> None:
    incoming, outgoing = pipe_streams
    outgoing.write(b"par")
    before_threads = set(threading.enumerate())
    before_tasks = asyncio.all_tasks()

    def forbidden_executor(*args, **kwargs):
        raise AssertionError("duplex input must not start executor work")

    monkeypatch.setattr(
        asyncio.get_running_loop(), "run_in_executor", forbidden_executor
    )
    async with DuplexLineReader(incoming, poll_interval=10) as reader:
        pending = asyncio.create_task(reader.read_line())
        await asyncio.sleep(0.02)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, timeout=0.5)

        outgoing.write(b"tial\n")
        assert await asyncio.wait_for(reader.read_line(), timeout=0.5) == b"partial"

    assert set(threading.enumerate()) == before_threads
    assert asyncio.all_tasks() == before_tasks


@pytest.mark.asyncio
async def test_concurrent_reads_are_rejected_without_consuming_input(
    pipe_streams: tuple[BinaryIO, BinaryIO],
) -> None:
    incoming, outgoing = pipe_streams
    async with DuplexLineReader(incoming) as reader:
        pending = asyncio.create_task(reader.read_line())
        await asyncio.sleep(0.02)
        with pytest.raises(RuntimeError, match="only one"):
            await reader.read_line()
        outgoing.write(b"run\n")
        assert await asyncio.wait_for(pending, timeout=1) == b"run"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["rb", "r"])
async def test_regular_file_uses_utf8_bytes_without_closing_borrowed_stream(
    mode: str, tmp_path: Path
) -> None:
    path = tmp_path / "input.jsonl"
    path.write_bytes("你好\ncontrol\n".encode("utf-8"))
    options = {"encoding": "ascii"} if mode == "r" else {}
    with path.open(mode, **options) as stream:
        async with DuplexLineReader(stream) as reader:
            assert await reader.read_line() == "你好".encode("utf-8")
            assert await reader.read_line() == b"control"
            assert await reader.read_line() is None
        assert not stream.closed


@pytest.mark.asyncio
async def test_integer_descriptor_is_borrowed_without_flag_changes(
    pipe_streams: tuple[BinaryIO, BinaryIO], monkeypatch: pytest.MonkeyPatch
) -> None:
    incoming, outgoing = pipe_streams
    descriptor = incoming.fileno()
    outgoing.write(b"run\n")

    def forbidden_set_blocking(*args):
        raise AssertionError("duplex input must not change descriptor flags")

    monkeypatch.setattr(os, "set_blocking", forbidden_set_blocking, raising=False)
    async with DuplexLineReader(descriptor) as reader:
        assert await asyncio.wait_for(reader.read_line(), timeout=1) == b"run"
    os.fstat(descriptor)


@pytest.mark.asyncio
async def test_closed_descriptor_is_reported_without_io_details(
    pipe_streams: tuple[BinaryIO, BinaryIO],
) -> None:
    incoming, _ = pipe_streams
    reader = DuplexLineReader(incoming)
    incoming.close()

    with pytest.raises(DuplexInputError, match="could not be read"):
        await reader.read_line()
    assert reader.closed


def test_unreadable_stream_is_rejected_at_construction() -> None:
    with pytest.raises(DuplexInputError):
        DuplexLineReader(-1)


@pytest.mark.parametrize("limit", [0, -1, False, 1.5])
def test_invalid_line_limits_are_rejected(limit) -> None:
    with pytest.raises((TypeError, ValueError)):
        DuplexLineReader(io.BytesIO(), max_line_bytes=limit)


@pytest.mark.parametrize("interval", [0, -1, False, float("nan"), float("inf"), "1"])
def test_invalid_poll_intervals_are_rejected(interval) -> None:
    with pytest.raises((TypeError, ValueError)):
        DuplexLineReader(io.BytesIO(), poll_interval=interval)


@pytest.mark.parametrize("shutdown", ["close", "cancel"])
def test_command_exits_while_parent_stdin_stays_open_without_leaking_threads(
    shutdown: str,
) -> None:
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    process = subprocess.Popen(
        [sys.executable, "-c", _OPEN_STDIN_CHILD, shutdown],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=PROJECT_ROOT,
        env=environment,
    )
    try:
        assert process.stdin is not None
        process.stdin.write(b'{"type":"run"}\n')
        process.stdin.flush()
        assert not process.stdin.closed

        process.wait(timeout=5)

        assert not process.stdin.closed
        process.stdin.close()
        process.stdin = None
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 0, stderr.decode("utf-8", errors="replace")
        assert (
            stdout == b"EXIT_WITH_STDIN_OPEN\r\n" or stdout == b"EXIT_WITH_STDIN_OPEN\n"
        )
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


def test_cold_import_does_not_load_runtime_or_machine_adapter() -> None:
    script = """
import sys
import jiuwenswarm.channels.process_cli.duplex_input

for name in sys.modules:
    assert not name.startswith('jiuwenswarm.runtime'), name
    assert name != 'jiuwenswarm.channels.process_cli.machine', name
print('NO_RUNTIME_IMPORTS')
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
    assert result.stdout.strip() == "NO_RUNTIME_IMPORTS"
