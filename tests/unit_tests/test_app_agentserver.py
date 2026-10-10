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
    captured: dict[str, asyncio.Event] = {}
    real_event = asyncio.Event

    def _event_factory() -> asyncio.Event:
        event = real_event()
        captured["ev"] = event
        return event

    def _fake_rmtree(path, *args, **kwargs) -> None:
        _ = args, kwargs
        removed_paths.append(Path(path))

    class _NoopSupervisor:
        async def stop(self) -> None:
            return None

    async def _fake_supervisor(front, host, port):
        _ = host, port
        front.attach_runtime_backend(_NoopSupervisor())
        captured["ev"].set()
        return _NoopSupervisor()

    monkeypatch.setattr(app_agentserver.asyncio, "Event", _event_factory)
    monkeypatch.setattr("shutil.rmtree", _fake_rmtree)
    monkeypatch.setattr(
        "jiuwenswarm.server.front.server.AgentServerFront",
        lambda host, port, **kwargs: fake_front,
    )
    monkeypatch.setattr(app_agentserver, "_start_supervisor", _fake_supervisor)

    await app_agentserver._run("127.0.0.1", 18092)

    assert fake_front.events[0] == "front_start"
    assert "attach" in fake_front.events
    assert "front_stop" in fake_front.events
    assert fake_front.events.index("front_start") < fake_front.events.index("attach")
    assert removed_paths == []
