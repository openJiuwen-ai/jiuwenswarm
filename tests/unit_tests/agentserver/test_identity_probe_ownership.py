# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.mark.asyncio
async def test_identity_helper_json_output_round_trips_without_log_prefix(monkeypatch, tmp_path, capsys):
    from jiuwenswarm.server.supervisor import identity, identity_cache, identity_probe

    observation = {"source": "技能/测试.py"}
    monkeypatch.setattr(identity, "code_identity", lambda: "a" * 64)
    monkeypatch.setattr(identity, "_identity_path", lambda: tmp_path / "absent-identity.json")
    monkeypatch.setattr(identity_cache, "launch_source_observation", lambda: observation)
    identity_probe.main()
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["code_identity"] == "a" * 64
    assert payload["source_observation"] == observation
    assert captured.err == ""

    process = SimpleNamespace(
        returncode=0,
        communicate=AsyncMock(return_value=(captured.out.encode("utf-8"), b"")),
        kill=Mock(),
        wait=AsyncMock(),
    )
    monkeypatch.setattr(identity_probe.sys, "frozen", False, raising=False)
    monkeypatch.setattr(identity_probe.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    assert await identity_probe.probe_identity() == ("a" * 64, observation)
    process.kill.assert_not_called()


@pytest.mark.asyncio
async def test_cancelling_identity_helper_kills_and_reaps_only_that_helper(monkeypatch):
    from jiuwenswarm.server.supervisor import identity_probe

    accepted = asyncio.Event()

    class Process:
        returncode = None
        kill = Mock()
        wait = AsyncMock()

        async def communicate(self):
            accepted.set()
            await asyncio.Future()

    process = Process()
    monkeypatch.setattr(identity_probe.sys, "frozen", False, raising=False)
    monkeypatch.setattr(identity_probe.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    task = asyncio.create_task(identity_probe.probe_identity())
    await accepted.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    process.kill.assert_called_once_with()
    process.wait.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_worker_starts_before_probe_and_failed_pipe_close_cannot_skip_owned_cleanup(monkeypatch):
    from jiuwenswarm.server.supervisor import identity, identity_probe, worker_process

    process = SimpleNamespace(stdin=SimpleNamespace(write=Mock(side_effect=BrokenPipeError("failed handoff")),
                                                    close=Mock(side_effect=OSError("failed close"))))
    popen = Mock(return_value=process)
    monkeypatch.setattr(worker_process.subprocess, "Popen", popen)

    async def probe():
        popen.assert_called_once()
        return "a" * 64, None

    monkeypatch.setattr(identity_probe, "probe_identity", probe)
    monkeypatch.setattr(identity, "launch_identity_payload", lambda *a, **k: b"identity")
    worker = worker_process.WorkerProcess("unused-pipe", host="127.0.0.1", port=1, stub=True)
    stop = AsyncMock()
    monkeypatch.setattr(worker, "stop", stop)
    with pytest.raises(BrokenPipeError, match="failed handoff"):
        await worker.spawn()
    stop.assert_awaited_once_with(timeout=5)
