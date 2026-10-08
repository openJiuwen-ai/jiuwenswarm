# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from pathlib import Path

import pytest

from jiuwenswarm.common.schema.designer_graph import (
    NODE_ROLE_CHARACTER_DESIGN,
    NODE_ROLE_SCENE,
    NODE_TYPE_IMAGE,
    SCHEMA_VERSION,
    normalize_execution_graph,
    normalize_execution_run,
)
from tests.unit_tests.designer.graph_fixtures import make_pipeline_graph
from jiuwenswarm.server.runtime.designer.a2a_collab import (
    DesignerA2ABus,
    align_specialists,
    collaborate_ready_wave,
    collaboration_card,
    review_storyboard_with_peers,
)


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.a2a_collab.get_agent_workspace_dir",
        lambda: tmp_path,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.common.get_agent_workspace_dir",
        lambda: tmp_path,
    )
    return tmp_path


def test_bus_records_a2a_shape() -> None:
    bus = DesignerA2ABus(context_id="run_a2a01")
    sent = bus.send(
        sender=NODE_ROLE_CHARACTER_DESIGN,
        recipient=NODE_ROLE_SCENE,
        text="角色穿湿漉漉的皮衣",
        task_id="align_1",
    )
    assert sent.context_id == "run_a2a01"
    assert sent.parts[0].text == "角色穿湿漉漉的皮衣"
    assert sent.sender in bus.transcript_markdown()


@pytest.mark.asyncio
async def test_align_specialists_writes_cards(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    async def fake_text(prompt: str, max_tokens: int = 800) -> str:
        calls.append(prompt)
        if "conflict" in prompt or "constraint" in prompt:
            return "- Costume must stay messenger bag\n- Keep morning station light"
        if "character designer" in prompt:
            return "# Character\nsmart-casual blazer, messenger bag"
        if "production designer" in prompt:
            return "# Scene\nmorning station, glass canopy"
        return "# other"

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.common.complete_designer_text",
        fake_text,
    )
    cards = await align_specialists(
        "火车站晨间",
        [NODE_ROLE_CHARACTER_DESIGN, NODE_ROLE_SCENE],
        run_id="run_align01",
    )
    assert NODE_ROLE_CHARACTER_DESIGN in cards
    assert NODE_ROLE_SCENE in cards
    assert "Constraints after aligning" in cards[NODE_ROLE_CHARACTER_DESIGN]
    assert collaboration_card("run_align01", NODE_ROLE_SCENE)
    assert (workspace / "designer_a2a_run_align01_transcript.md").is_file()
    assert any("A2A" in prompt or "colleague" in prompt.lower() for prompt in calls)


@pytest.mark.asyncio
async def test_collaborate_ready_wave_runs_for_character_and_scene(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_text(prompt: str, max_tokens: int = 800) -> str:
        return "card:" + prompt[20:40]

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.common.complete_designer_text",
        fake_text,
    )
    graph = make_pipeline_graph(project_id="proj_a2a01", prompt="火车站")
    if not any(node.get("id") == "n_scene" for node in graph["nodes"]):
        graph["nodes"].append(
            {
                "id": "n_scene",
                "type": NODE_TYPE_IMAGE,
                "label": "Scene",
                "config": {"role": NODE_ROLE_SCENE, "inputs": ["n_brief"]},
            }
        )
    graph = normalize_execution_graph(graph)
    run = normalize_execution_run(
        {
            "schema_version": "designer-execution-run.v1",
            "run_id": "run_wave01",
            "graph_id": graph["graph_id"],
            "project_id": "proj_a2a01",
            "status": "running",
            "node_states": {
                "n_brief": {"status": "completed"},
                "n_character": {"status": "pending"},
                "n_scene": {"status": "pending"},
            },
            "current_node_ids": ["n_character", "n_scene"],
        }
    )
    cards = await collaborate_ready_wave(graph, run, ["n_character", "n_scene"])
    assert set(cards) == {NODE_ROLE_CHARACTER_DESIGN, NODE_ROLE_SCENE}
    again = await collaborate_ready_wave(graph, run, ["n_character", "n_scene"])
    assert again[NODE_ROLE_SCENE] == cards[NODE_ROLE_SCENE]


@pytest.mark.asyncio
async def test_collaborate_skipped_when_disabled(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = {"n": 0}

    async def fake_text(prompt: str, max_tokens: int = 800) -> str:
        called["n"] += 1
        return "nope"

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.common.complete_designer_text",
        fake_text,
    )
    graph = normalize_execution_graph(
        {
            "schema_version": SCHEMA_VERSION,
            "graph_id": "graph_off01",
            "project_id": "proj_off01",
            "title": "off",
            "description": "x",
            "source": "manual",
            "nodes": [
                {
                    "id": "n_character",
                    "type": "image",
                    "label": "c",
                    "config": {"role": NODE_ROLE_CHARACTER_DESIGN, "collaborate": False},
                },
                {
                    "id": "n_scene",
                    "type": "image",
                    "label": "s",
                    "config": {"role": NODE_ROLE_SCENE, "collaborate": False},
                },
            ],
            "edges": [],
        }
    )
    run = normalize_execution_run(
        {
            "schema_version": "designer-execution-run.v1",
            "run_id": "run_off01",
            "graph_id": "graph_off01",
            "project_id": "proj_off01",
            "status": "running",
            "node_states": {
                "n_character": {"status": "pending"},
                "n_scene": {"status": "pending"},
            },
            "current_node_ids": [],
        }
    )
    assert await collaborate_ready_wave(graph, run, ["n_character", "n_scene"]) == {}
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_review_storyboard_keeps_table_when_peers_ok(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace.joinpath("designer_a2a_run_rev01_character_design.md").write_text(
        "# 角色\n皮衣", encoding="utf-8"
    )
    workspace.joinpath("designer_a2a_run_rev01_scene.md").write_text(
        "# 场景\n玻璃站台", encoding="utf-8"
    )

    async def fake_text(prompt: str, max_tokens: int = 800) -> str:
        return "OK"

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.common.complete_designer_text",
        fake_text,
    )
    table = (
        "| 镜号 | 时间轴 | 镜头视角 | 运镜 | 人物变化 | 场景变化 |\n"
        "| --- | --- | --- | --- | --- | --- |\n"
        "| 1 | 0-5s | 全景 | 固定 | 未入画 | 站台 |\n"
    )
    assert await review_storyboard_with_peers(table, run_id="run_rev01") == table
