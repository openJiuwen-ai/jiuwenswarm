# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from unittest.mock import patch

from jiuwenswarm.server.runtime.agent_manager import AgentManager
from jiuwenswarm.server.runtime.tenant_agent_pool import TenantAgentPool


class _FakeSwarm:
    def __init__(self, catalog: list[dict[str, str]]) -> None:
        self._catalog = catalog

    def get_registered_tools_catalog(self) -> list[dict[str, str]]:
        return list(self._catalog)


def _make_manager(*swarms: _FakeSwarm) -> AgentManager:
    manager = AgentManager(agent_id="tenant-a", service_id="service-a")
    manager.agents = {
        "officeclaw": {
            f"agent:{index}": swarm
            for index, swarm in enumerate(swarms)
        }
    }
    return manager


def test_iter_jiuwenswarm_instances_uses_current_two_level_cache() -> None:
    swarm_a = _FakeSwarm([])
    swarm_b = _FakeSwarm([])
    manager = _make_manager(swarm_a, swarm_b)
    manager.agents["malformed"] = "not-a-dict"  # type: ignore[assignment]

    iterator = getattr(manager, "iter_jiuwenswarm_instances", None)
    assert callable(iterator)
    assert iterator() == [swarm_a, swarm_b]


def test_collect_runtime_tools_catalog_nowait_unions_initialized_managers() -> None:
    manager_one = _make_manager(
        _FakeSwarm(
            [{"name": "bash", "description": "Run shell.", "short_description": "Run shell."}]
        )
    )
    manager_two = _make_manager(
        _FakeSwarm(
            [
                {
                    "name": "read_file",
                    "description": "Read files.",
                    "short_description": "Read files.",
                }
            ]
        ),
        _FakeSwarm(
            [
                {
                    "name": "bash",
                    "description": "Run shell commands safely.",
                    "short_description": "Run shell commands safely.",
                }
            ]
        ),
    )
    pool = TenantAgentPool()

    with patch.object(pool, "iter_agent_managers_nowait", return_value=[manager_one, manager_two]):
        catalog = pool.collect_runtime_tools_catalog_nowait()

    assert set(catalog) == {"bash", "read_file"}
    assert catalog["bash"]["short_description"] == "Run shell commands safely."


def test_collect_runtime_tools_catalog_nowait_does_not_create_managers() -> None:
    pool = TenantAgentPool()

    with patch.object(pool, "iter_agent_managers_nowait", return_value=[]) as iter_managers:
        assert pool.collect_runtime_tools_catalog_nowait() == {}

    iter_managers.assert_called_once_with()
