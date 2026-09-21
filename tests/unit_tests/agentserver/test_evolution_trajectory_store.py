# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for skill-evolution trajectory span processor wiring."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jiuwenswarm.common.utils import get_agent_evolution_trajectories_dir, get_agent_root_dir
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


def test_get_agent_evolution_trajectories_dir_under_agent_root():
    assert get_agent_evolution_trajectories_dir() == get_agent_root_dir() / "evolution_trajectories"


def test_build_skill_evolution_rail_passes_trajectory_span_processor(tmp_path, monkeypatch):
    processor = object()
    captured: dict = {}

    class _FakeSkillEvolutionRail:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    adapter = JiuWenSwarmDeepAdapter()
    adapter._model = object()
    adapter._default_model_name = "gpt-test"
    adapter._skill_manager = SimpleNamespace(list_execution_disabled_skills=lambda: [])
    with (
        patch(
            "openjiuwen.extensions.observability.demand.get_trajectory_span_processor",
            return_value=processor,
        ),
        patch.object(adapter, "_resolve_skill_dirs", return_value=[str(tmp_path / "skills")]),
        patch(
            "jiuwenswarm.server.runtime.agent_adapter.interface_deep.SkillEvolutionRail",
            _FakeSkillEvolutionRail,
        ),
        patch(
            "jiuwenswarm.server.runtime.agent_adapter.interface_deep.EvolutionReviewRuntime",
            return_value=object(),
        ),
    ):
        rail = adapter._build_skill_evolution_rail(
            {"react": {"evolution": {"enabled": True}}}
        )

    assert rail is not None
    assert captured.get("trajectory_span_processor") is processor


@pytest.mark.asyncio
async def test_ensure_active_evolution_rails_passes_trajectory_span_processor(tmp_path, monkeypatch):
    processor = object()
    configure = AsyncMock()

    adapter = JiuWenSwarmDeepAdapter()
    adapter._instance = object()
    adapter._model = object()
    adapter._default_model_name = "gpt-test"
    adapter._config_cache = {"react": {"evolution": {"enabled": True}}}
    adapter._skill_manager = SimpleNamespace(list_execution_disabled_skills=lambda: [])
    adapter._skill_evolution_rail = SimpleNamespace(_language="cn")

    with (
        patch.object(adapter, "_resolve_runtime_language", return_value="cn"),
        patch.object(adapter, "_resolve_skill_dirs", return_value=[str(tmp_path / "skills")]),
        patch(
            "openjiuwen.extensions.observability.demand.get_trajectory_span_processor",
            return_value=processor,
        ),
        patch(
            "jiuwenswarm.server.runtime.agent_adapter.interface_deep.configure_skill_evolution_runtime",
            configure,
        ),
        patch.object(adapter, "_refresh_active_evolution_rail_refs", MagicMock()),
        patch(
            "jiuwenswarm.server.runtime.agent_adapter.interface_deep._set_skill_evolution_triggers",
            MagicMock(),
        ),
    ):
        await adapter._ensure_active_evolution_rails_registered()

    configure.assert_awaited_once()
    kwargs = configure.await_args.kwargs
    assert kwargs.get("trajectory_span_processor") is processor
