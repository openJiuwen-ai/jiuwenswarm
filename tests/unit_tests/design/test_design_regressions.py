# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Consolidated regression tests (merged from 9 small test modules)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import pytest
from jiuwenswarm.common.schema.designer_graph import normalize_execution_graph
from jiuwenswarm.server.runtime.design.context import StoreNodeSpawner
from jiuwenswarm.common.schema.designer_graph import (
    compose_required_predecessor_ids,
    is_compose_sink_node,
)
from jiuwenswarm.server.runtime.designer.pipeline.clip_staging_lock import (
    build_action_lock,
    build_positioning_lock,
    build_relationship_lock,
    enrich_clip_staging,
    staging_lock_clause,
)
from typing import Any
from jiuwenswarm.agents.harness.design.rails.designer_authoring_rail import (
    AUTORUN_FAILURE_HINT,
    DesignerAuthoringRail,
)
from jiuwenswarm.server.runtime.design.director_chain import DirectorChainError
from jiuwenswarm.server.runtime.designer.model_tools import DesignerLlmError
from openjiuwen.core.foundation.tool import ToolCard
from jiuwenswarm.agents.harness.design.rails.designer_surface_sanitizer_rail import (
    DesignerSurfaceSanitizerRail,
)
from jiuwenswarm.server.runtime.designer.pipeline.model_capacity import capacity_for_model, snap_resolution
import asyncio
from jiuwenswarm.server.runtime.designer import model_tools, script_analysis
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.runtime.gateway_adapter import session_adapter
from pathlib import Path
from jiuwenswarm.server.runtime.design import team_bridge
from jiuwenswarm.server.runtime.design.context import get_design_context_registry


# --- from test_agent_graph_lock.py ---

@pytest.mark.parametrize("active", [True, False])
def test_agent_edit_is_blocked_only_while_a_trigger_is_active(active):
    graph = normalize_execution_graph({
        "schema_version": "designer-execution-graph.v1",
        "graph_id": "g_locked", "project_id": "p_locked", "title": "before", "nodes": [], "edges": [],
    })
    store = SimpleNamespace(get_graph=Mock(return_value=graph), save_graph=Mock(side_effect=lambda g: g))
    coordinator = SimpleNamespace(is_active=lambda: active)
    pushed = Mock()
    spawner = StoreNodeSpawner(store, "g_locked", lambda: coordinator, on_graph_update=pushed)
    if active:
        with pytest.raises(RuntimeError, match="executing"):
            spawner.apply_agent_graph_patch("g_locked", {"title": "after"})
        store.save_graph.assert_not_called()
        pushed.assert_not_called()
    else:
        result = spawner.apply_agent_graph_patch("g_locked", {"title": "after"})
        assert result["title"] == "after"
        store.save_graph.assert_called_once()
        pushed.assert_called_once()

@pytest.mark.asyncio
async def test_node_run_reports_a_trigger_refused_before_the_node_started():
    refused = {
        "status": "failed",
        "error": "ValueError: upstream not ready: n_brief",
        "node_states": {"n_music": {"status": "pending"}},
    }
    store = SimpleNamespace(get_graph=Mock(return_value={"graph_id": "g_bgm"}))
    coordinator = SimpleNamespace(is_active=lambda: False, start=AsyncMock(return_value=refused))
    spawner = StoreNodeSpawner(store, "g_bgm", lambda: coordinator)

    result = await spawner.spawn_node_agent("run_bgm", "n_music")

    assert result == "ValueError: upstream not ready: n_brief"

# --- from test_clip_staging_lock.py ---

def test_positioning_action_relationship_locks_from_shot():
    characters = [
        {"id": "char_1", "name": "Alice"},
        {"id": "char_2", "name": "Bob"},
    ]
    shot = {
        "clip_index": 2,
        "setting_id": "set_1",
        "camera": "medium front view",
        "view_key": "front",
        "on_screen": ["char_1", "char_2"],
        "cast_actions": {
            "char_1": "standing, holding phone, speaking",
            "char_2": "sitting across the table, smiling",
        },
        "speech_line": "Shall we order?",
        "speaker": "char_1",
        "looks_at": {"char_1": "char_2", "char_2": "char_1"},
        "blocking": {
            "landmark": "candlelit table",
            "positions": [
                {
                    "character_id": "char_1",
                    "name": "Alice",
                    "zone": "left",
                    "facing": "toward Bob",
                    "pose": "standing",
                    "near": "Bob",
                },
                {
                    "character_id": "char_2",
                    "name": "Bob",
                    "zone": "right",
                    "facing": "toward Alice",
                    "pose": "sitting",
                },
            ],
        },
    }
    pos = build_positioning_lock(shot, characters)
    act = build_action_lock(shot, characters)
    rel = build_relationship_lock(shot, characters)
    assert "camera=medium front view" in pos
    assert "Alice@zone=left" in pos
    assert "standing" in act and "sitting" in act
    assert "looks_at" in rel
    assert "talks_to" in rel or "speaks" in rel
    assert "next_to" in rel or "adjacent" in rel

    locks = enrich_clip_staging(shot, characters)
    clause = staging_lock_clause(
        positioning_lock=locks["positioning_lock"],
        action_lock=locks["action_lock"],
        relationship_lock=locks["relationship_lock"],
        clip_index=2,
        setting_id="set_1",
        for_clip=True,
    )
    assert "STAGING LOCK" in clause
    assert "POSITIONING" in clause
    assert "ACTION/POSTURE" in clause
    assert "RELATIONSHIPS" in clause
    assert "STAGING PRIORITY" in clause
    assert "EQUAL to clothing" in clause

def test_concurrency_soft_clip_hard_compose_unchanged():
    graph = {
        "nodes": [
            {"id": "n_clip_1", "type": "video", "config": {"role": "clip", "clip_index": 1}},
            {
                "id": "n_clip_2",
                "type": "video",
                "config": {
                    "role": "clip",
                    "clip_index": 2,
                    "previous_clip_handoff_ready": True,
                    "previous_clip_action": "prior beat",
                },
            },
            {"id": "n_compose", "type": "video", "config": {"role": "compose"}},
            {"id": "n_speech", "type": "audio", "config": {"role": "speech"}},
        ],
        "edges": [
            {"source": "n_clip_1", "target": "n_clip_2"},
            {"source": "n_clip_1", "target": "n_compose"},
            {"source": "n_clip_2", "target": "n_compose"},
            {"source": "n_speech", "target": "n_compose"},
        ],
    }
    assert is_compose_sink_node(graph["nodes"][2]) is True
    preds = set(compose_required_predecessor_ids(graph))
    assert "n_clip_1" in preds and "n_clip_2" in preds and "n_speech" in preds

# --- from test_designer_authoring_rail.py ---

class _FakeStore:
    def get_graph(self, graph_id: str) -> dict[str, Any]:
        return {"graph_id": graph_id, "nodes": [], "edges": [], "metadata": {}}

def _rail(monkeypatch: pytest.MonkeyPatch) -> DesignerAuthoringRail:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.graph_store.DesignerGraphStore",
        _FakeStore,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.model_tools.require_llm",
        lambda: None,
    )
    rail = DesignerAuthoringRail(session_id="sess_authoring_test")
    rail._graph_id = "g_authoring_test"
    return rail

@pytest.mark.asyncio
async def test_chain_failure_tells_leader_not_to_hand_build(monkeypatch):
    async def fail(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise DirectorChainError("模型未返回可用的角色/镜头分析")

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.design.director_chain.run_director_chain",
        fail,
    )
    tool = _rail(monkeypatch)._build_tool()

    result = await tool.invoke({"prompt": "给这段音乐配一个 5 秒的视频"})

    assert result.startswith("error: 模型未返回可用的角色/镜头分析")
    assert result.endswith(AUTORUN_FAILURE_HINT)

@pytest.mark.asyncio
async def test_missing_model_tells_leader_not_to_hand_build(monkeypatch):
    rail = _rail(monkeypatch)

    def no_model() -> None:
        raise DesignerLlmError("未配置对话模型")

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.model_tools.require_llm",
        no_model,
    )
    tool = rail._build_tool()

    result = await tool.invoke({"prompt": "做一个短片"})

    assert result == f"error: 未配置对话模型\n{AUTORUN_FAILURE_HINT}"

# --- from test_designer_surface_sanitizer_rail.py ---

class _FakeAbilityManager:
    def __init__(self, names: list[str]) -> None:
        self._cards = {name: ToolCard(name=name, description=name) for name in names}

    def list(self) -> list[ToolCard]:
        return list(self._cards.values())

    def remove_ability(self, name: str) -> None:
        del self._cards[name]

    def names(self) -> set[str]:
        return set(self._cards)

class _FakeAgent:
    def __init__(self, ability_manager: _FakeAbilityManager) -> None:
        self.ability_manager = ability_manager
        self.system_prompt_builder = None

def test_sanitizer_keeps_web_tools_and_drops_team_tools() -> None:
    ability_manager = _FakeAbilityManager(
        ["free_search", "fetch_webpage", "paid_search", "pcs_search", "designer_graph_get", "send_message", "workspace_meta"]
    )

    DesignerSurfaceSanitizerRail().init(_FakeAgent(ability_manager))

    assert ability_manager.names() == {"free_search", "fetch_webpage", "paid_search", "designer_graph_get"}

# --- from test_execution_dependency_order.py ---

@pytest.mark.asyncio
async def test_compose_handler_refuses_while_clip_still_running(
    tmp_path, monkeypatch
):
    """ffmpeg must not assemble a 0:00 film while clips are still generating."""
    from jiuwenswarm.common.schema.designer_graph import (
        NODE_ROLE_CLIP,
        NODE_ROLE_COMPOSE,
        NODE_STATUS_COMPLETED,
        NODE_STATUS_RUNNING,
        NODE_TYPE_VIDEO,
        SCHEMA_VERSION,
        normalize_execution_graph,
    )
    from jiuwenswarm.server.runtime.designer.handlers.common import file_output_ref
    from jiuwenswarm.server.runtime.designer.handlers.compose import ComposeNodeHandler
    from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext

    clip1 = tmp_path / "shot1.mp4"
    clip1.write_bytes(b"m" * 600)
    graph = normalize_execution_graph(
        {
            "schema_version": SCHEMA_VERSION,
            "graph_id": "graph_compose_wait",
            "project_id": "proj_compose_wait",
            "title": "成片",
            "nodes": [
                {
                    "id": "n_clip_1",
                    "type": NODE_TYPE_VIDEO,
                    "config": {"role": NODE_ROLE_CLIP, "clip_index": 1},
                },
                {
                    "id": "n_clip_2",
                    "type": NODE_TYPE_VIDEO,
                    "config": {"role": NODE_ROLE_CLIP, "clip_index": 2},
                },
                {
                    "id": "n_compose",
                    "type": NODE_TYPE_VIDEO,
                    "config": {"role": NODE_ROLE_COMPOSE, "inputs": ["n_clip_1", "n_clip_2"]},
                },
            ],
            "edges": [
                {"id": "e1", "source": "n_clip_1", "target": "n_compose"},
                {"id": "e2", "source": "n_clip_2", "target": "n_compose"},
            ],
        }
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.common.get_agent_workspace_dir",
        lambda: tmp_path,
    )

    async def _fast_sleep(*_a, **_k):
        return None

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.compose.asyncio.sleep",
        _fast_sleep,
    )

    with pytest.raises(RuntimeError, match="waiting for all clips|clip n_clip_2"):
        await ComposeNodeHandler().execute(
            graph["nodes"][-1],
            NodeExecutionContext(
                graph=graph,
                run_id="run_compose_wait",
                node_id="n_compose",
                run={
                    "node_states": {
                        "n_clip_1": {
                            "status": NODE_STATUS_COMPLETED,
                            "output_ref": file_output_ref(
                                clip1, kind=NODE_TYPE_VIDEO, mime_type="video/mp4"
                            ),
                        },
                        "n_clip_2": {"status": NODE_STATUS_RUNNING},
                    }
                },
            ),
        )

# --- from test_resolution_fallback.py ---

@pytest.mark.parametrize("model,requested,expected", [
    ("MiniMax-H3", "480p", "768P"),
    ("MiniMax-H3", "480", "768P"),
    ("MiniMax-H3", "720P", "768P"),
    ("MiniMax-H3", "4K", "2K"),
    ("MiniMax-H3", "2K", "2K"),
    ("MiniMax-H3", "", "768P"),
    ("MiniMax-H3-Max", "480P", "480P"),
    ("wan3.0-video", "480P", "480P"),
])
def test_resolution_snaps_to_the_nearest_lower_or_minimum_tier(model, requested, expected):
    assert snap_resolution(requested, capacity_for_model(model)) == expected

@pytest.mark.parametrize("model", [
    "MiniMax-H3-Highspeed",
    "minimax/MiniMax-H3-Highspeed-01",
    "MiniMax-H3-Lite",
    "MiniMax H3 Lite",
    "MiniMax-H3-Max",
])
def test_minimax_restricted_h3_variants_offer_only_supported_tiers(model):
    capacity = capacity_for_model(model)
    assert capacity.resolutions == ("480P", "768P")
    assert capacity.default_resolution == "768P"
    assert snap_resolution("1080p", capacity) == "768P"
    assert snap_resolution("2K", capacity) == "768P"

# --- from test_script_analysis_budget.py ---

@pytest.mark.asyncio
async def test_script_analysis_timeout_reports_first_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_model_call(**kwargs: object) -> dict[str, object]:
        raise asyncio.TimeoutError

    monkeypatch.setattr(model_tools, "call_model_tool", fake_model_call)

    with pytest.raises(DesignerLlmError) as excinfo:
        await script_analysis.analyze_creative_brief("A short film.", timeout_sec=5)

    message = str(excinfo.value)
    assert "during script analysis (no reply arrived in time)" in message
    assert "after 120s" not in message

@pytest.mark.asyncio
async def test_script_analysis_timeout_reports_reinforce_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []

    async def fake_model_call(**kwargs: object) -> dict[str, object]:
        calls.append(True)
        if len(calls) == 1:
            return {"ok": True, "text": "not a json object"}
        raise asyncio.TimeoutError

    monkeypatch.setattr(model_tools, "call_model_tool", fake_model_call)

    with pytest.raises(DesignerLlmError) as excinfo:
        await script_analysis.analyze_creative_brief("A short film.", timeout_sec=5)

    assert len(calls) == 2
    assert "the first reply was not usable JSON and the retry did not finish in time" in str(excinfo.value)

# --- from test_session_adapter_work_mode.py ---

@pytest.mark.asyncio
async def test_mode_filtered_session_list_scans_metadata_once(monkeypatch):
    calls: list[tuple[int, int]] = []

    def list_metadata(*, limit: int, offset: int):
        calls.append((limit, offset))
        return [
            {"session_id": "work", "work_mode": "work"},
            {"session_id": "design", "work_mode": "design"},
        ], 2

    monkeypatch.setattr(
        session_adapter,
        "get_all_sessions_metadata",
        list_metadata,
    )
    request = AgentRequest(
        request_id="request",
        channel_id="web",
        params={"work_mode": "design", "limit": 20, "offset": 0},
    )

    response = await session_adapter.SessionAdapter()._handle_list(request)

    assert response.ok is True
    assert response.payload is not None
    assert response.payload["total"] == 1
    assert [item["session_id"] for item in response.payload["sessions"]] == ["design"]
    assert calls == [(10**9, 0)]

# --- from test_team_bridge_context.py ---

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

