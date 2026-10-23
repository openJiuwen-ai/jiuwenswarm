# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Ordinary CLI completion must wait for history and metadata persistence."""

from __future__ import annotations

import argparse
import asyncio
import io
import threading
import time

import pytest

from jiuwenswarm.channels.process_cli import app
from jiuwenswarm.channels.process_cli.client import InProcessRuntimeClient
from jiuwenswarm.runtime.events import RuntimeEvent
from jiuwenswarm.server.runtime.session import session_history, session_metadata


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_failure", [False, True])
async def test_client_close_drains_both_writers_after_runtime_close(
    monkeypatch, runtime_failure
):
    calls = []
    both_started = threading.Barrier(2, timeout=3)

    class RuntimeStub:
        async def close(self):
            calls.append("runtime")
            if runtime_failure:
                raise RuntimeError("runtime close failed")

    def flush(timeout):
        assert calls[:1] == ["runtime"]
        assert timeout > 0
        both_started.wait()
        calls.append("flush")
        return True

    monkeypatch.setattr(session_history, "flush_pending_writes", flush)
    monkeypatch.setattr(session_metadata, "flush_pending_writes", flush)
    client = InProcessRuntimeClient(RuntimeStub())
    if runtime_failure:
        with pytest.raises(RuntimeError, match="runtime close failed"):
            await client.close()
    else:
        await client.close()
    assert calls == ["runtime", "flush", "flush"]


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_failure", [False, True])
@pytest.mark.parametrize("session_cancelled", [False, True])
async def test_command_drains_writers_after_slow_close_consumes_its_window(
    monkeypatch, tmp_path, runtime_failure, session_cancelled
):
    written = [threading.Event(), threading.Event()]
    files = [tmp_path / "history.jsonl", tmp_path / "metadata.json"]

    class RuntimeStub:
        async def start(self):
            pass

        async def create_or_resume_session(self, *, channel_id, session_id):
            return "process_cli_slow_close"

        async def stream(self, request, *, on_agent_ready=None):
            yield RuntimeEvent(
                request_id=request.request_id,
                channel_id=request.channel_id,
                session_id=request.session_id,
                payload={"event_type": "chat.final", "content": "PERSISTED_REPLY"},
                is_complete=True,
            )

        async def cleanup_session(self, *, channel_id, session_id):
            if session_cancelled:
                raise asyncio.CancelledError
            return True

        async def close(self):
            await asyncio.sleep(0.1)
            if runtime_failure:
                raise RuntimeError("runtime close failed")

    def flush_history(_timeout):
        time.sleep(0.4)
        files[0].write_text("PERSISTED_REPLY", encoding="utf-8")
        written[0].set()
        return True

    def flush_metadata(_timeout):
        time.sleep(0.4)
        files[1].write_text("process_cli_slow_close", encoding="utf-8")
        written[1].set()
        return True

    client = InProcessRuntimeClient(RuntimeStub())
    monkeypatch.setattr(app, "InProcessRuntimeClient", lambda: client)
    monkeypatch.setattr(app, "SHUTDOWN_STEP_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(session_history, "flush_pending_writes", flush_history)
    monkeypatch.setattr(session_metadata, "flush_pending_writes", flush_metadata)
    args = argparse.Namespace(
        prompt="reply",
        mode="agent.code.normal",
        work_mode="normal",
        session=None,
        cwd=str(tmp_path),
        project_dir=None,
        trusted_dir=[],
        timeout=3,
        output="jsonl",
        show_reasoning=False,
        show_tools=False,
    )
    stdout, stderr = io.StringIO(), io.StringIO()
    try:
        if session_cancelled:
            with pytest.raises(asyncio.CancelledError):
                await app.run(args, stdout=stdout, stderr=stderr)
        else:
            assert await app.run(args, stdout=stdout, stderr=stderr) == 0, (
                stdout.getvalue() + stderr.getvalue()
            )
        assert files[0].read_text(encoding="utf-8") == "PERSISTED_REPLY"
        assert files[1].read_text(encoding="utf-8") == "process_cli_slow_close"
    finally:
        await asyncio.gather(*(asyncio.to_thread(event.wait, 2) for event in written))
