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


class _FakeServer:
    def __init__(self) -> None:
        self.agent_manager = object()
        self.started_with_bind: bool | None = None

    async def start(self, *, bind_transport: bool = True) -> None:
        self.started_with_bind = bind_transport

    async def stop(self) -> None:
        return None

    def get_agent_manager(self) -> object:
        return self.agent_manager

    def schedule_image_modality_warmup(self, *, reason: str) -> None:
        _ = reason

    async def send_push(self, payload: object) -> None:
        _ = payload


@pytest.mark.asyncio
async def test_run_does_not_delete_agent_teams_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    removed_paths: list[Path] = []
    fake_front = _FakeFront("127.0.0.1", 18092)
    fake_server = _FakeServer()
    captured: dict[str, asyncio.Event] = {}
    real_event = asyncio.Event

    def _event_factory() -> asyncio.Event:
        event = real_event()
        captured["ev"] = event
        return event

    def _fake_rmtree(path, *args, **kwargs) -> None:
        _ = args, kwargs
        removed_paths.append(Path(path))

    def _fake_spawn(_stop_event, _server) -> asyncio.Task:
        async def _noop() -> None:
            return None

        return asyncio.create_task(_noop())

    async def _fake_backend(front, host, port):
        _ = host, port
        await fake_server.start(bind_transport=False)
        front.attach_runtime_backend(fake_server)
        captured["ev"].set()
        return fake_server

    monkeypatch.setattr(app_agentserver.asyncio, "Event", _event_factory)
    monkeypatch.setattr("shutil.rmtree", _fake_rmtree)
    monkeypatch.setattr(
        "jiuwenswarm.server.front.server.AgentServerFront",
        lambda host, port, **kwargs: fake_front,
    )
    monkeypatch.setattr(app_agentserver, "_start_runtime_backend", _fake_backend)
    monkeypatch.setattr(app_agentserver, "_spawn_teammate_bootstrap", _fake_spawn)

    await app_agentserver._run("127.0.0.1", 18092)

    assert fake_front.events[0] == "front_start"
    assert "attach" in fake_front.events
    assert "front_stop" in fake_front.events
    assert fake_front.events.index("front_start") < fake_front.events.index("attach")
    assert fake_server.started_with_bind is False
    assert removed_paths == []
