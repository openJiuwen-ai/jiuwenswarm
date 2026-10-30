# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression tests for ``swarm.plugin_rails`` per-member instance isolation.

The ``swarm.plugin_rails`` element is declared as "a fresh instance of every
registered rail extension, one per member". These tests pin that contract: two
members must never share a rail extension object, and neither may alias the
main agent's cached instance.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from jiuwenswarm.agents.harness.common.plugins import rail_manager as rail_manager_mod
from jiuwenswarm.agents.swarm.providers import member_rails

_RAIL_SOURCE = '''\
from openjiuwen.harness.rails.base import DeepAgentRail


class DemoRail(DeepAgentRail):
    """Demo extension rail used by the isolation test."""

    priority: int = 50

    def __init__(self):
        super().__init__()
        self.seen = []
'''


class _StubAgent:
    """Minimal DeepAgent stand-in for RailManager hot-reload bookkeeping."""

    def __init__(self) -> None:
        self.rails: list = []

    async def register_rail(self, rail) -> None:
        self.rails.append(rail)

    async def unregister_rail(self, rail) -> None:
        self.rails.remove(rail)


@pytest.fixture()
def manager_with_demo_extension(tmp_path, monkeypatch):
    """A RailManager rooted in ``tmp_path`` with one registered demo rail."""
    monkeypatch.setattr(
        rail_manager_mod,
        "get_agent_workspace_dir",
        lambda: tmp_path,
    )
    monkeypatch.setattr(rail_manager_mod.RailManager, "_instance", None)
    monkeypatch.setattr(rail_manager_mod.RailManager, "_extensions", {})

    source_dir: Path = tmp_path / "source" / "demo"
    source_dir.mkdir(parents=True)
    (source_dir / "rail.py").write_text(_RAIL_SOURCE, encoding="utf-8")

    manager = rail_manager_mod.RailManager()
    manager.import_extension(str(source_dir))

    # Mirror the main agent's startup path: the extension is hot-registered on
    # the host DeepAgent, which caches one shared instance.
    agent = _StubAgent()
    manager.set_agent_instance(agent)
    asyncio.run(manager.hot_reload_rail("demo", True))
    assert manager.get_registered_rail_names() == {"demo"}

    monkeypatch.setattr(member_rails, "get_rail_manager", lambda: manager)
    return manager, agent


def test_plugin_rails_builds_a_distinct_instance_per_member(
    manager_with_demo_extension,
):
    """Each member build must get its own rail object, not a shared one."""
    manager, agent = manager_with_demo_extension

    member_a = member_rails._build_plugin_rails({}, None)
    member_b = member_rails._build_plugin_rails({}, None)

    assert len(member_a) == 1
    assert len(member_b) == 1
    assert member_a[0] is not member_b[0], (
        "swarm.plugin_rails returned the same rail object to two members"
    )
    assert member_a[0] is not agent.rails[0], (
        "swarm.plugin_rails aliased the main agent's cached rail instance"
    )
    assert member_b[0] is not agent.rails[0]


def test_plugin_rails_state_does_not_leak_between_members(
    manager_with_demo_extension,
):
    """Per-member rail state must stay isolated."""
    manager, agent = manager_with_demo_extension

    member_a = member_rails._build_plugin_rails({}, None)[0]
    member_b = member_rails._build_plugin_rails({}, None)[0]

    member_a.seen.append("member-a-secret")

    assert member_b.seen == []
    assert agent.rails[0].seen == []
