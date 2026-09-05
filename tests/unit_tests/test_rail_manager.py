from __future__ import annotations

import asyncio
import builtins
from typing import Any

import pytest

from jiuwenswarm.agents.harness.common.plugins import (
    rail_manager as rail_manager_module,
)
from jiuwenswarm.agents.harness.common.plugins.rail_manager import (
    RailExtension,
    RailManager,
)

_RAIL_NAME = "sample_rail"


class _FakeAgent:
    def __init__(self) -> None:
        self.registered: list[Any] = []
        self.unregistered: list[Any] = []

    async def register_rail(self, rail: Any) -> None:
        self.registered.append(rail)

    async def unregister_rail(self, rail: Any) -> None:
        self.unregistered.append(rail)


class _FailingUnregisterAgent(_FakeAgent):
    async def unregister_rail(self, rail: Any) -> None:
        self.unregistered.append(rail)
        raise RuntimeError("unregister failed")


@pytest.fixture
def rail_manager(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> RailManager:
    monkeypatch.setattr(
        rail_manager_module,
        "get_agent_workspace_dir",
        lambda: tmp_path,
    )
    RailManager._instance = None
    manager = RailManager()
    manager._extensions = {
        _RAIL_NAME: RailExtension(
            name=_RAIL_NAME,
            class_name="SampleRail",
            enabled=True,
        )
    }
    yield manager
    RailManager._instance = None


@pytest.mark.asyncio
async def test_hot_reload_registration_is_scoped_per_agent(
    rail_manager: RailManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[object] = []

    def _fresh(_name: str) -> object:
        rail = object()
        created.append(rail)
        return rail

    monkeypatch.setattr(rail_manager, "create_fresh_rail_instance", _fresh)
    template_agent = _FakeAgent()
    session_agent = _FakeAgent()

    await rail_manager.hot_reload_rail(
        _RAIL_NAME,
        True,
        agent_instance=template_agent,
    )
    await rail_manager.hot_reload_rail(
        _RAIL_NAME,
        True,
        agent_instance=session_agent,
    )
    await rail_manager.hot_reload_rail(
        _RAIL_NAME,
        True,
        agent_instance=session_agent,
    )

    assert len(created) == 2
    assert template_agent.registered == [created[0]]
    assert session_agent.registered == [created[1]]
    assert created[0] is not created[1]
    assert rail_manager.is_rail_registered(
        _RAIL_NAME,
        agent_instance=template_agent,
    )
    assert rail_manager.is_rail_registered(
        _RAIL_NAME,
        agent_instance=session_agent,
    )

    await rail_manager.hot_reload_rail(
        _RAIL_NAME,
        False,
        agent_instance=template_agent,
    )
    assert template_agent.unregistered == [created[0]]
    assert rail_manager.is_rail_registered(_RAIL_NAME)

    await rail_manager.hot_reload_rail(
        _RAIL_NAME,
        False,
        agent_instance=session_agent,
    )
    assert session_agent.unregistered == [created[1]]
    assert not rail_manager.is_rail_registered(_RAIL_NAME)


@pytest.mark.asyncio
async def test_concurrent_agents_do_not_share_hot_reload_target(
    rail_manager: RailManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        rail_manager,
        "create_fresh_rail_instance",
        lambda _name: object(),
    )
    first = _FakeAgent()
    second = _FakeAgent()

    await asyncio.gather(
        rail_manager.hot_reload_rail(
            _RAIL_NAME,
            True,
            agent_instance=first,
        ),
        rail_manager.hot_reload_rail(
            _RAIL_NAME,
            True,
            agent_instance=second,
        ),
    )

    assert len(first.registered) == 1
    assert len(second.registered) == 1
    assert first.registered[0] is not second.registered[0]


@pytest.mark.asyncio
async def test_repeated_set_agent_instance_keeps_registration_idempotent(
    rail_manager: RailManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[object] = []

    def _fresh(_name: str) -> object:
        rail = object()
        created.append(rail)
        return rail

    monkeypatch.setattr(rail_manager, "create_fresh_rail_instance", _fresh)
    agent = _FakeAgent()

    rail_manager.set_agent_instance(agent)
    await rail_manager.hot_reload_rail(_RAIL_NAME, True)
    rail_manager.set_agent_instance(agent)
    await rail_manager.hot_reload_rail(_RAIL_NAME, True)

    assert len(created) == 1
    assert agent.registered == created


def test_fresh_instances_reuse_cached_class_until_invalidated(
    rail_manager: RailManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded_classes: list[type] = []

    def _load_uncached(_name: str) -> type:
        rail_class = type("SampleRail", (), {})
        loaded_classes.append(rail_class)
        return rail_class

    monkeypatch.setattr(rail_manager, "_load_rail_class_uncached", _load_uncached)

    first = rail_manager.create_fresh_rail_instance(_RAIL_NAME)
    second = rail_manager.create_fresh_rail_instance(_RAIL_NAME)

    assert first is not second
    assert type(first) is type(second)
    assert len(loaded_classes) == 1

    rail_manager._rail_instances[_RAIL_NAME] = object()
    rail_manager.invalidate_rail_cache(_RAIL_NAME)
    third = rail_manager.create_fresh_rail_instance(_RAIL_NAME)

    assert type(third) is not type(first)
    assert len(loaded_classes) == 2
    assert _RAIL_NAME not in rail_manager._rail_instances


def test_package_module_executes_once_until_cache_is_invalidated(
    rail_manager: RailManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extension_dir = rail_manager._extensions_dir / _RAIL_NAME
    extension_dir.mkdir()
    (extension_dir / "__init__.py").write_text(
        "from .rail import SampleRail\n",
        encoding="utf-8",
    )
    (extension_dir / "rail.py").write_text(
        "import builtins\n"
        "builtins._rail_test_exec_count += 1\n"
        "class SampleRail:\n"
        "    pass\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(builtins, "_rail_test_exec_count", 0, raising=False)

    try:
        first = rail_manager.create_fresh_rail_instance(_RAIL_NAME)
        second = rail_manager.create_fresh_rail_instance(_RAIL_NAME)

        assert first is not second
        assert type(first) is type(second)
        assert getattr(builtins, "_rail_test_exec_count") == 1

        rail_manager.invalidate_rail_cache(_RAIL_NAME)
        third = rail_manager.create_fresh_rail_instance(_RAIL_NAME)

        assert type(third) is not type(first)
        assert getattr(builtins, "_rail_test_exec_count") == 2
    finally:
        rail_manager.invalidate_rail_cache(_RAIL_NAME)


@pytest.mark.asyncio
async def test_delete_extension_unregisters_every_agent_before_removal(
    rail_manager: RailManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        rail_manager,
        "create_fresh_rail_instance",
        lambda _name: object(),
    )
    first = _FakeAgent()
    second = _FakeAgent()
    await rail_manager.hot_reload_rail(_RAIL_NAME, True, agent_instance=first)
    await rail_manager.hot_reload_rail(_RAIL_NAME, True, agent_instance=second)
    rail_manager._rail_classes[_RAIL_NAME] = type("SampleRail", (), {})
    rail_manager._rail_instances[_RAIL_NAME] = object()

    assert await rail_manager.delete_extension(_RAIL_NAME) is True

    assert first.unregistered == first.registered
    assert second.unregistered == second.registered
    assert _RAIL_NAME not in rail_manager._extensions
    assert _RAIL_NAME not in rail_manager._rail_classes
    assert _RAIL_NAME not in rail_manager._rail_instances
    assert not rail_manager.is_rail_registered(_RAIL_NAME)


@pytest.mark.asyncio
async def test_delete_extension_preserves_failed_registration_for_retry(
    rail_manager: RailManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        rail_manager,
        "create_fresh_rail_instance",
        lambda _name: object(),
    )
    healthy = _FakeAgent()
    failing = _FailingUnregisterAgent()
    await rail_manager.hot_reload_rail(_RAIL_NAME, True, agent_instance=healthy)
    await rail_manager.hot_reload_rail(_RAIL_NAME, True, agent_instance=failing)
    rail_manager._rail_classes[_RAIL_NAME] = type("SampleRail", (), {})
    cached_instance = object()
    rail_manager._rail_instances[_RAIL_NAME] = cached_instance

    with pytest.raises(RuntimeError, match="已保留扩展以便重试"):
        await rail_manager.delete_extension(_RAIL_NAME)

    assert healthy.unregistered == healthy.registered
    assert failing.unregistered == failing.registered
    assert not rail_manager.is_rail_registered(
        _RAIL_NAME,
        agent_instance=healthy,
    )
    assert rail_manager.is_rail_registered(
        _RAIL_NAME,
        agent_instance=failing,
    )
    assert _RAIL_NAME in rail_manager._extensions
    assert _RAIL_NAME in rail_manager._rail_classes
    assert rail_manager._rail_instances[_RAIL_NAME] is cached_instance
