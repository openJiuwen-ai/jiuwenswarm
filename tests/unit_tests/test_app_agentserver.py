# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from jiuwenswarm.server import app_agentserver
from jiuwenswarm.server.lifecycle import Readiness


class _FakeFront:
    def __init__(self, host: str, port: int, **_kwargs) -> None:
        self.host = host
        self.port = port
        self.readiness = Readiness()
        self.events: list[str] = []

    async def start(self) -> None:
        self.readiness.mark_transport_ready()
        self.readiness.mark_control_ready()
        self.readiness.mark_runtime_warming()
        self.events.append("front_start")

    async def stop(self) -> None:
        self.events.append("front_stop")

    def attach_runtime_backend(self, backend: object) -> None:
        self.events.append("attach")
        _ = backend


@pytest.mark.asyncio
async def test_run_does_not_delete_agent_teams_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    removed_paths: list[Path] = []
    fake_front = _FakeFront("127.0.0.1", 18092)
    events = []
    caller = asyncio.current_task()

    def _fake_rmtree(path, *args, **kwargs) -> None:
        _ = args, kwargs
        removed_paths.append(Path(path))

    class _NoopSupervisor:
        def __init__(self, *args, **kwargs):
            pass

        async def run_until_ipc_ready(self):
            events.append("worker_started")
            asyncio.get_running_loop().call_later(.05, caller.cancel)

        async def stop(self):
            events.append("detached")

        async def abort_startup(self):
            events.append("aborted")

    monkeypatch.setattr("jiuwenswarm.server.supervisor.runtime_supervisor.RuntimeSupervisor", _NoopSupervisor)
    monkeypatch.setattr("shutil.rmtree", _fake_rmtree)
    monkeypatch.setattr(
        "jiuwenswarm.server.front.server.AgentServerFront",
        lambda host, port, **kwargs: fake_front,
    )

    await app_agentserver._run("127.0.0.1", 18092)

    assert events == ["worker_started", "detached"]
    assert "attach" in fake_front.events
    assert "front_stop" in fake_front.events
    assert fake_front.events.index("attach") < fake_front.events.index("front_start")
    assert removed_paths == []
