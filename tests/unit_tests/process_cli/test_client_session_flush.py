# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Ordinary CLI completion must wait for history and metadata persistence."""

from __future__ import annotations

import threading

import pytest

from jiuwenswarm.channels.process_cli.client import InProcessRuntimeClient
from jiuwenswarm.server.runtime.session import session_history, session_metadata


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_failure", [False, True])
async def test_client_close_drains_both_writers_after_runtime_close(
    monkeypatch, runtime_failure
):
    calls = []
    both_started = threading.Barrier(2, timeout=3)

    class RuntimeStub:
        async def cancel_all_team_stream_tasks(self, *, reason):
            calls.append("team")

        async def close(self):
            calls.append("runtime")
            if runtime_failure:
                raise RuntimeError("runtime close failed")

    def flush(timeout):
        assert calls[:2] == ["team", "runtime"]
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
    assert calls == ["team", "runtime", "flush", "flush"]
