# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.server import app_agentserver


async def _run_agentserver_for_test(host: str, port: int) -> None:
    await getattr(app_agentserver, "_run")(host, port)


@pytest.mark.asyncio
async def test_run_does_not_delete_agent_teams_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    removed_paths: list[Path] = []
    server_events: list[str] = []

    class _FakeEvent:
        @staticmethod
        def set() -> None:
            return None

        async def wait(self) -> None:
            return None

    class _FakeServer:
        async def start(self) -> None:
            server_events.append("start")

        async def stop(self) -> None:
            server_events.append("stop")

    class _FakeExtensionManager:
        def __init__(self, registry) -> None:
            self.registry = registry

        async def load_all_extensions(self) -> None:
            return None

        @staticmethod
        def list_extensions() -> list[object]:
            return []

    async def _fake_bootstrap_daemon(*, stop_event) -> None:
        _ = stop_event
        return None

    def _fake_rmtree(path, *args, **kwargs) -> None:
        _ = args, kwargs
        removed_paths.append(Path(path))

    monkeypatch.setattr(app_agentserver.asyncio, "Event", _FakeEvent)
    monkeypatch.setattr("shutil.rmtree", _fake_rmtree)
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.remote_member_bootstrap.run_teammate_bootstrap_daemon",
        _fake_bootstrap_daemon,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.agent_ws_server.AgentWebSocketServer.get_instance",
        staticmethod(lambda **_kwargs: _FakeServer()),
    )
    monkeypatch.setattr(
        "jiuwenswarm.extensions.registry.ExtensionRegistry.create_instance",
        staticmethod(lambda **_kwargs: object()),
    )
    monkeypatch.setattr(
        "jiuwenswarm.extensions.manager.ExtensionManager",
        _FakeExtensionManager,
    )
    monkeypatch.setattr(
        "openjiuwen.core.runner.Runner.callback_framework",
        SimpleNamespace(),
    )

    await _run_agentserver_for_test("127.0.0.1", 18092)

    assert server_events == ["start", "stop"]
    assert removed_paths == []
