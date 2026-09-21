# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Integration tests for declared Agents at the existing manager boundary."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.server.runtime.agent_manager import AgentManager


class _Facade:
    instances: list[_Facade] = []

    def __init__(self) -> None:
        self.create_kwargs: dict[str, Any] = {}
        self.cleaned = False
        type(self).instances.append(self)

    def set_heartbeat_service(self, _service: object) -> None:
        return None

    def set_personal_context_runtime_enabled(self, _enabled: bool) -> None:
        return None

    def set_permissions_changed_notifier(self, _notifier: object) -> None:
        return None

    def set_permissions_external_input_context_builder(self, _builder: object) -> None:
        return None

    async def create_instance(self, _config: object, **kwargs: Any) -> None:
        self.create_kwargs = kwargs

    async def cleanup(self) -> None:
        self.cleaned = True


class _FailingFacade(_Facade):
    async def create_instance(self, _config: object, **_kwargs: Any) -> None:
        raise RuntimeError("declared build failed")


@pytest.fixture(autouse=True)
def _clear_facade_instances() -> None:
    _Facade.instances.clear()
    _FailingFacade.instances.clear()


@pytest.mark.asyncio
async def test_declared_agent_has_isolated_cache_and_immutable_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.server.runtime.agent_adapter import interface

    monkeypatch.setattr(interface, "JiuWenSwarm", _Facade)
    manager = AgentManager()
    definition: dict[str, Any] = {
        "name": "isolated_agent",
        "instructions": "Keep this snapshot.",
        "tools": "*",
        "skills": ["review"],
    }
    try:
        declared = await manager.get_agent(
            channel_id="process_cli",
            mode="code",
            agent_definition=definition,
            agent_definition_fingerprint="a" * 64,
        )
        declared_again = await manager.get_agent(
            channel_id="process_cli",
            mode="code",
            agent_definition=definition,
            agent_definition_fingerprint="a" * 64,
        )
        default = await manager.get_agent(channel_id="process_cli", mode="code")
        definition["skills"].append("mutated")

        assert declared is declared_again
        assert default is not declared
        assert len(_Facade.instances) == 2
        assert _Facade.instances[0].create_kwargs["agent_definition"]["skills"] == [
            "review"
        ]
        assert "agent_definition" not in _Facade.instances[1].create_kwargs
        declared_key = next(
            key for key in manager._agent_create_params["process_cli"] if ":agent:" in key
        )
        assert manager._agent_create_params["process_cli"][declared_key][
            "agent_definition"
        ]["skills"] == ["review"]
    finally:
        await manager.cleanup()


@pytest.mark.asyncio
async def test_declared_agent_creation_failure_cleans_partial_facade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.server.runtime.agent_adapter import interface

    monkeypatch.setattr(interface, "JiuWenSwarm", _FailingFacade)
    manager = AgentManager()

    with pytest.raises(RuntimeError, match="declared build failed"):
        await manager.get_agent(
            channel_id="process_cli",
            mode="code",
            agent_definition={
                "name": "failing_agent",
                "instructions": "Fail after allocation.",
                "tools": "*",
            },
            agent_definition_fingerprint="b" * 64,
        )

    assert len(_FailingFacade.instances) == 1
    assert _FailingFacade.instances[0].cleaned is True
    assert not manager.agents.get("process_cli")


@pytest.mark.asyncio
async def test_skillnet_reload_preserves_declared_agent_snapshot() -> None:
    from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm

    facade = object.__new__(JiuWenSwarm)
    adapter = SimpleNamespace(create_instance=AsyncMock())
    facade._sdk_name = "test"
    facade._session_manager = SimpleNamespace(has_active_tasks=lambda: False)
    facade._runtime_agent_create_snapshot = None
    facade._ensure_adapter = lambda *, mode: adapter
    facade._reload_team_skill_rails = AsyncMock()
    config = {"nested": {"value": "original"}}
    definition = {
        "name": "snapshot_agent",
        "instructions": "Keep this declaration.",
        "skills": ["review"],
    }

    await facade.create_instance(
        config,
        mode="code",
        sub_mode="normal",
        agent_definition=definition,
    )
    config["nested"]["value"] = "mutated"
    definition["skills"].append("mutated")
    await facade._on_skillnet_install_complete()

    reload_call = adapter.create_instance.await_args_list[1]
    assert reload_call.args[0] == {"nested": {"value": "original"}}
    assert reload_call.kwargs == {
        "mode": "code",
        "sub_mode": "normal",
        "agent_definition": {
            "name": "snapshot_agent",
            "instructions": "Keep this declaration.",
            "skills": ["review"],
        },
    }
    facade._reload_team_skill_rails.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_skillnet_reload_keeps_default_agent_no_argument_contract() -> None:
    from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm

    facade = object.__new__(JiuWenSwarm)
    facade._runtime_agent_create_snapshot = None
    facade.create_instance = AsyncMock()
    facade._reload_team_skill_rails = AsyncMock()

    await JiuWenSwarm._on_skillnet_install_complete(facade)

    facade.create_instance.assert_awaited_once_with()
    facade._reload_team_skill_rails.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_failed_declared_rebuild_keeps_last_valid_skillnet_snapshot() -> None:
    from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm

    facade = object.__new__(JiuWenSwarm)
    adapter = SimpleNamespace(create_instance=AsyncMock())
    facade._sdk_name = "test"
    facade._session_manager = SimpleNamespace(has_active_tasks=lambda: False)
    facade._runtime_agent_create_snapshot = None
    facade._ensure_adapter = lambda *, mode: adapter
    original = {
        "name": "original_agent",
        "instructions": "Keep the last valid definition.",
    }
    await facade.create_instance(mode="code", agent_definition=original)
    saved = facade._runtime_agent_create_snapshot
    adapter.create_instance.side_effect = RuntimeError("rebuild failed")

    with pytest.raises(RuntimeError, match="rebuild failed"):
        await facade.create_instance(
            mode="code",
            agent_definition={
                "name": "broken_agent",
                "instructions": "This build fails.",
            },
        )

    assert facade._runtime_agent_create_snapshot == saved
