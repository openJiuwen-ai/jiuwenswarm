from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from jiuwenswarm.agents.harness.common.tools.deepresearch import diagnostics as dd


def _capture(tmp_path: Path, conversation_id: str = "C-log") -> dd.RunnerDiagnostics:
    return dd.RunnerDiagnostics(
        tmp_path / "位置 with spaces" / "DeepResearch" / "output" / "logs",
        conversation_id, "request-id",
    )


def test_stderr_journal_rejects_relative_or_wrong_sdk_layout(tmp_path, caplog):
    for path in [Path("relative/output/logs"), tmp_path / "wrong-layout"]:
        capture = dd.RunnerDiagnostics(path, "C-log", "request-id")
        capture.prepare()
        capture.close()
        assert capture.cwd is None and capture.stderr_path is None
        assert capture.failed
    assert "诊断准备失败" in caplog.text


def test_stderr_journal_uses_exclusive_private_binary_creation(tmp_path):
    original = _capture(tmp_path)
    original.attempt_id = "fixed-attempt"
    stamp = datetime(2026, 11, 13, tzinfo=timezone.utc)
    with patch.object(dd, "datetime", Mock(now=Mock(return_value=stamp))):
        original.prepare()
        original.write(b"first\xff\n")
        original.close()
        duplicate = _capture(tmp_path)
        duplicate.attempt_id = original.attempt_id
        duplicate.prepare()
        duplicate.write(b"must-not-overwrite")
        duplicate.close()
    assert duplicate.failed
    assert original.stderr_path.read_bytes() == b"first\xff\n"


def test_stderr_journal_handles_short_writes_and_closes_after_disk_failure(tmp_path, caplog):
    capture = _capture(tmp_path, "../../unsafe conversation: query")
    capture.prepare()
    underlying = capture._stream

    class Writer:
        broken = False

        def write(self, value):
            if self.broken:
                raise OSError("sensitive device detail must not enter host logs")
            return underlying.write(value[:3])

        def flush(self):
            return underlying.flush()

        def close(self):
            return underlying.close()

    writer = Writer()
    capture._stream = writer
    capture.write(b"first cause\xff")
    writer.broken = True
    capture.write(b"disk failure")
    capture.write(b"ignored after failure")
    capture.close()
    assert capture.stderr_path.read_bytes() == b"first cause\xff"
    assert capture.bytes_written == len(b"first cause\xff")
    assert underlying.closed
    assert capture.failed
    assert "日志写入失败" in caplog.text
    assert "unsafe conversation" not in str(capture.stderr_path)
    assert "sensitive device detail" not in caplog.text


@pytest.mark.asyncio
async def test_concurrent_attempts_do_not_share_files_or_block_event_loop(tmp_path):
    captures = await asyncio.gather(*(
        dd.prepare_runner_diagnostics(str(_capture(tmp_path).sdk_log_dir), "same-conversation", "request-id")
        for _ in range(8)
    ))
    await asyncio.gather(*(
        asyncio.to_thread(capture.write, f"attempt-{index}".encode())
        for index, capture in enumerate(captures)
    ))
    await asyncio.gather(*(dd.close_runner_diagnostics(capture) for capture in captures))
    paths = [capture.stderr_path for capture in captures]
    assert len(set(paths)) == 8
    assert [path.read_bytes() for path in paths] == [f"attempt-{index}".encode() for index in range(8)]


def test_failed_prepare_is_reported_as_disabled_in_start_log(tmp_path, caplog):
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("preserve me", encoding="utf-8")
    capture = dd.RunnerDiagnostics(blocked / "DeepResearch" / "output" / "logs", "C-log", "request-id")
    caplog.set_level(logging.INFO)
    capture.prepare()
    capture.started(42)
    capture.close()
    assert capture.cwd is None
    assert "stderr_enabled=False prepare_failed=True" in caplog.text
    assert blocked.read_text(encoding="utf-8") == "preserve me"


@pytest.mark.asyncio
async def test_stalled_write_disables_capture_without_stalling_next_drain_chunk(tmp_path):
    capture = _capture(tmp_path)
    capture.prepare()
    release, finished = threading.Event(), threading.Event()
    original_write = capture.write

    def slow_write(chunk):
        release.wait(timeout=5)
        original_write(chunk)
        finished.set()

    try:
        with patch.object(capture, "write", slow_write), patch.object(dd, "_WRITE_TIMEOUT_SECONDS", 0.03):
            await asyncio.wait_for(dd.write_runner_diagnostics(capture, b"first"), timeout=1)
            assert capture.failed and dd._WRITE_TASKS
            # 下一块不再等待故障磁盘，宿主仍能排空真实子进程的管道。
            await asyncio.wait_for(dd.write_runner_diagnostics(capture, b"next"), timeout=0.1)
            release.set()
            assert await asyncio.wait_for(asyncio.to_thread(finished.wait, 2), timeout=3)
    finally:
        release.set()
        await dd.close_runner_diagnostics(capture)
