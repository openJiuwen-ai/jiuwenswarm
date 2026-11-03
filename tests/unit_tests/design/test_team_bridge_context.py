# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Cold-registry binding for canvas reruns (ensure_design_context)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jiuwenswarm.server.runtime.design import team_bridge
from jiuwenswarm.server.runtime.design.context import get_design_context_registry


class _WorkerSpec:
    agents = {"teammate": "base-spec"}
    build_context = "build-ctx"
    team_name = "ut-team"


class _FakeTeamManager:
    def __init__(self) -> None:
        self.calls = 0

    async def get_swarm_enriched_team_spec(self, **_kwargs: Any) -> _WorkerSpec:
        self.calls += 1
        return _WorkerSpec()


async def test_ensure_design_context_binds_a_cold_registry(
    monkeypatch, tmp_path: Path
) -> None:
    graph = {"graph_id": "ut-bridge-g1", "metadata": {"session_id": "ut-bridge-s1"}}
    fake = _FakeTeamManager()
    monkeypatch.setattr(
        team_bridge, "ensure_designer_agent_group_installed", lambda: None
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.get_team_manager", lambda: fake
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.common.canvas_output_dir",
        lambda _graph: tmp_path,
    )
    registry = get_design_context_registry()
    try:
        await team_bridge.ensure_design_context(graph)

        context = registry.for_graph("ut-bridge-g1")
        assert context is not None
        assert context.worker_base_spec == "base-spec"
        assert context.worker_build_context == "build-ctx"
        assert context.team_name == "ut-team"
        # Idempotent: a second warm-up does not re-derive the spec.
        await team_bridge.ensure_design_context(graph)
        assert fake.calls == 1
    finally:
        registry.drop("ut-bridge-s1")
