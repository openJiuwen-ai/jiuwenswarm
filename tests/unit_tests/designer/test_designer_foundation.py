# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for Designer graph store and bootstrap schema."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.designer.handlers.common import graph_prompt
from jiuwenswarm.common.schema.designer_graph import (
    CONFIG_DELEGATE_HANDLER,
    EDGE_KIND_SYNC,
    NODE_ROLE_CHARACTER_DESIGN,
    NODE_ROLE_CLIP,
    NODE_ROLE_COMPOSE,
    NODE_ROLE_FRAME,
    NODE_ROLE_SCENE,
    NODE_ROLE_STORYBOARD,
    NODE_TYPE_IMAGE,
    NODE_TYPE_TABLE,
    NODE_TYPE_TEXT,
    NODE_TYPE_VIDEO,
    NODE_STATUS_COMPLETED,
    NODE_STATUS_RUNNING,
    RUN_STATUS_COMPLETED,
    RUN_STATUS_RUNNING,
    DesignerExecutionGraph,
    DesignerGraphValidationError,
    apply_graph_patch,
    apply_shot_generate_prompts,
    build_bootstrap_graph,
    expand_clip_nodes_for_shots,
    preserve_expanded_shot_nodes,
    normalize_execution_graph,
    normalize_node,
    node_pipeline,
)


def _handler_graph(graph: DesignerExecutionGraph) -> DesignerExecutionGraph:
    """Keep legacy DAG executor tests on the handler escape hatch."""
    for node in graph.get("nodes") or []:
        config = node.setdefault("config", {})
        config["delegate"] = CONFIG_DELEGATE_HANDLER
        config["force_handler"] = True
        config["skip_llm"] = True
    return graph
from jiuwenswarm.server.runtime.designer.executor import GraphExecutor
from jiuwenswarm.server.runtime.designer.graph_store import DesignerGraphStore


def _assert_nodes_do_not_overlap(nodes: list) -> None:
    boxes = []
    for node in nodes:
        layout = node.get("layout") or {}
        x = float(layout.get("x") or 0)
        y = float(layout.get("y") or 0)
        width = float(layout.get("width") or 280)
        height = float(layout.get("height") or 160)
        boxes.append((str(node.get("id") or ""), x, y, x + width, y + height))
    for index, left in enumerate(boxes):
        for right in boxes[index + 1 :]:
            overlap_x = left[1] < right[3] and right[1] < left[3]
            overlap_y = left[2] < right[4] and right[2] < left[4]
            assert not (overlap_x and overlap_y), f"{left[0]} overlaps {right[0]}"


@pytest.fixture()
def stub_clip_video(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_text(prompt: str, max_tokens: int = 1200) -> str:
        if "分镜" in prompt or "运镜" in prompt or "Storyboard" in prompt:
            return (
                "## 分镜表\n\n"
                "| 镜号 | 时间轴 | 镜头视角 | 运镜 | 人物变化 | 场景变化 |\n"
                "| --- | --- | --- | --- | --- | --- |\n"
                "| 1 | 0.0-2.0s | 全景/平视 | 缓摇 | 未入画 | 站台 |\n"
                "| 2 | 2.0-3.5s | 中景/平视 | 跟移 | 主体入画 | 出站 |\n"
                "| 3 | 3.5-5.0s | 近景/平视 | 固定 | 转身 | 月台 |\n"
            )
        return f"# stub\n{prompt[:80]}"

    image_seq = {"n": 0}

    async def fake_image(
        prompt: str,
        size: str = "512x512",
        reference_image: str | None = None,
        reference_images: list[str] | None = None,
        **kwargs,
    ) -> dict[str, str]:
        image_seq["n"] += 1
        path = tmp_path / f"generated_{image_seq['n']}.png"
        path.write_bytes(b"png")
        return {"image_path": str(path)}

    video_seq = {"n": 0}

    async def fake_video(
        prompt: str,
        save_dir: str | None = None,
        first_frame: str | None = None,
        reference_images: list[str] | None = None,
        **kwargs,
    ) -> dict[str, str]:
        video_seq["n"] += 1
        path = tmp_path / f"generated_clip_{video_seq['n']}.mp4"
        path.write_bytes(b"mp4" * 200)
        return {"video_path": str(path), "revised_prompt": prompt}

    def fake_concat(paths: list[Path], dest: Path) -> Path:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"mp4-merged" * 80)
        return dest.resolve()

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.common.get_agent_workspace_dir",
        lambda: tmp_path,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.common.complete_designer_text",
        fake_text,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.common.generate_designer_image",
        fake_image,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.clip.generate_clip_video",
        fake_video,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.compose.concatenate_clip_videos",
        fake_concat,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.model_tools.require_llm",
        lambda: None,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.model_tools.llm_available",
        lambda: True,
    )


@pytest.fixture()
def designer_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_director_llm: None
) -> DesignerGraphStore:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.graph_store.get_agent_root_dir",
        lambda: tmp_path,
    )
    return DesignerGraphStore()


def test_bootstrap_graph_uses_modality_node_types() -> None:
    graph = build_bootstrap_graph(
        project_id="proj_test01",
        prompt="test prompt",
        title="Test Video",
    )
    assert graph["schema_version"] == "designer-execution-graph.v1"
    node_types = {node["type"] for node in graph["nodes"]}
    assert node_types <= {NODE_TYPE_TEXT, NODE_TYPE_TABLE, NODE_TYPE_IMAGE, NODE_TYPE_VIDEO}
    assert NODE_TYPE_TEXT in node_types
    assert NODE_TYPE_IMAGE in node_types
    roles = {node_pipeline(node) for node in graph["nodes"]}
    assert {NODE_ROLE_CHARACTER_DESIGN, NODE_ROLE_STORYBOARD, NODE_ROLE_SCENE} <= roles
    assert any(edge["source"] == "n_brief" and edge["target"] == "n_scene" for edge in graph["edges"])
    sync_edges = [edge for edge in graph["edges"] if edge.get("kind") == EDGE_KIND_SYNC]
    assert len(sync_edges) == 2
    sync_pairs = {frozenset((edge["source"], edge["target"])) for edge in sync_edges}
    assert sync_pairs == {
        frozenset({"n_character", "n_storyboard"}),
        frozenset({"n_scene", "n_storyboard"}),
    }
    assert not any(node_pipeline(node) == NODE_ROLE_FRAME for node in graph["nodes"])
    assert any(edge["source"] == "n_storyboard" and edge["target"] == "n_clip_1" for edge in graph["edges"])
    assert any(edge["source"] == "n_clip_1" and edge["target"] == "n_compose" for edge in graph["edges"])
    clip = next(node for node in graph["nodes"] if node["id"] == "n_clip_1")
    compose = next(node for node in graph["nodes"] if node["id"] == "n_compose")
    assert node_pipeline(compose) == NODE_ROLE_COMPOSE
    assert "n_clip_1" in ((compose.get("config") or {}).get("inputs") or [])
    clip_layout = clip.get("layout") or {}
    compose_layout = compose.get("layout") or {}
    assert compose_layout["x"] >= clip_layout["x"] + clip_layout["width"]
    _assert_nodes_do_not_overlap(graph["nodes"])


def test_graph_store_roundtrip(designer_store: DesignerGraphStore) -> None:
    graph = build_bootstrap_graph(project_id="proj_test01", prompt="roundtrip")
    saved = designer_store.save_graph(graph)
    loaded = designer_store.get_graph(saved["graph_id"])
    assert loaded is not None
    assert loaded["graph_id"] == saved["graph_id"]
    assert loaded["project_id"] == "proj_test01"


def test_fixture_file_normalizes() -> None:
    fixture = (
        Path(__file__).resolve().parents[3]
        / "jiuwenswarm"
        / "channels"
        / "web"
        / "frontend"
        / "tests"
        / "fixtures"
        / "designer-execution-graph.v1.json"
    )
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    graph = normalize_execution_graph(payload)
    assert graph["title"] == "示例短视频"
    assert any(edge["source"] == "n_frame_1" and edge["target"] == "n_clip_1" for edge in graph["edges"])
    assert any(edge["source"] == "n_clip_1" and edge["target"] == "n_compose" for edge in graph["edges"])


def test_expand_clip_nodes_for_shots_creates_one_clip_per_shot() -> None:
    graph = build_bootstrap_graph(project_id="proj_expand01", prompt="three shots")
    expanded = expand_clip_nodes_for_shots(graph, 3)
    clip_ids = [node["id"] for node in expanded["nodes"] if node_pipeline(node) == NODE_ROLE_CLIP]
    frame_ids = [node["id"] for node in expanded["nodes"] if node_pipeline(node) == NODE_ROLE_FRAME]
    assert clip_ids == ["n_clip_1", "n_clip_2", "n_clip_3"]
    assert frame_ids == []
    compose = next(node for node in expanded["nodes"] if node["id"] == "n_compose")
    assert node_pipeline(compose) == NODE_ROLE_COMPOSE
    assert (compose.get("config") or {}).get("inputs") == ["n_clip_1", "n_clip_2", "n_clip_3"]
    assert any(edge["source"] == "n_clip_2" and edge["target"] == "n_compose" for edge in expanded["edges"])
    assert any(edge["source"] == "n_storyboard" and edge["target"] == "n_clip_3" for edge in expanded["edges"])
    clips = [node for node in expanded["nodes"] if node_pipeline(node) == NODE_ROLE_CLIP]
    compose_layout = compose.get("layout") or {}
    clip_right = max(
        float((node.get("layout") or {}).get("x") or 0)
        + float((node.get("layout") or {}).get("width") or 280)
        for node in clips
    )
    assert compose_layout["x"] >= clip_right
    _assert_nodes_do_not_overlap(expanded["nodes"])


def test_apply_shot_generate_prompts_fills_clips_and_keeps_user_edits() -> None:
    graph = expand_clip_nodes_for_shots(
        build_bootstrap_graph(project_id="proj_prompt01", prompt="fill prompts"),
        2,
    )
    filled = apply_shot_generate_prompts(
        graph,
        ["火车进站的全景", "年轻人从车门走出"],
    )
    clip1 = next(node for node in filled["nodes"] if node["id"] == "n_clip_1")
    clip2 = next(node for node in filled["nodes"] if node["id"] == "n_clip_2")
    assert clip1["config"]["generate"]["prompt"] == "火车进站的全景"
    assert clip2["config"]["generate"]["prompt"] == "年轻人从车门走出"

    clip1["config"]["generate"]["prompt"] = "用户改过的画面"
    clip1["config"]["generate"]["prompt_origin"] = "user"
    kept = apply_shot_generate_prompts(
        filled,
        ["新的分镜注释", "白领从地铁门走出"],
    )
    kept_clip1 = next(node for node in kept["nodes"] if node["id"] == "n_clip_1")
    kept_clip2 = next(node for node in kept["nodes"] if node["id"] == "n_clip_2")
    assert kept_clip1["config"]["generate"]["prompt"] == "用户改过的画面"
    assert kept_clip2["config"]["generate"]["prompt"] == "白领从地铁门走出"


def test_preserve_node_output_refs_keeps_storyboard_uri() -> None:
    from jiuwenswarm.common.schema.designer_graph import preserve_node_output_refs

    existing = build_bootstrap_graph(project_id="proj_keep_ref", prompt="keep ref")
    story = next(node for node in existing["nodes"] if node["id"] == "n_storyboard")
    story["output_ref"] = {
        "kind": "table",
        "uri": "file:///tmp/storyboard.md",
        "mime_type": "text/markdown",
        "label": "storyboard.md",
    }
    incoming = build_bootstrap_graph(project_id="proj_keep_ref", prompt="keep ref")
    incoming["graph_id"] = existing["graph_id"]
    merged = preserve_node_output_refs(incoming, existing)
    kept = next(node for node in merged["nodes"] if node["id"] == "n_storyboard")
    assert kept["output_ref"]["uri"] == "file:///tmp/storyboard.md"


def test_expand_preserves_existing_generate_prompt() -> None:
    graph = build_bootstrap_graph(project_id="proj_keep_prompt", prompt="keep prompt")
    clip = next(node for node in graph["nodes"] if node["id"] == "n_clip_1")
    clip["config"]["generate"] = {
        "prompt": "用户改过的画面",
        "prompt_origin": "user",
    }
    expanded = expand_clip_nodes_for_shots(graph, 2)
    kept = next(node for node in expanded["nodes"] if node["id"] == "n_clip_1")
    assert kept["config"]["generate"]["prompt"] == "用户改过的画面"
    assert kept["config"]["generate"]["prompt_origin"] == "user"


def test_normalize_repairs_overlapping_compose_and_column_layout() -> None:
    graph = build_bootstrap_graph(project_id="proj_layout01", prompt="old overlap")
    layouts = {
        "n_character": {"x": 380, "y": 40, "width": 280, "height": 160},
        "n_scene": {"x": 380, "y": 160, "width": 280, "height": 160},
        "n_storyboard": {"x": 380, "y": 320, "width": 280, "height": 160},
        "n_clip_1": {"x": 1040, "y": 160, "width": 280, "height": 160},
        "n_compose": {"x": 1100, "y": 160, "width": 280, "height": 160},
    }
    for node in graph["nodes"]:
        layout = layouts.get(str(node.get("id") or ""))
        if layout:
            node["layout"] = dict(layout)
    restored = normalize_execution_graph(graph)
    compose = next(node for node in restored["nodes"] if node["id"] == "n_compose")
    clip = next(node for node in restored["nodes"] if node["id"] == "n_clip_1")
    clip_layout = clip.get("layout") or {}
    compose_layout = compose.get("layout") or {}
    assert compose_layout["x"] >= float(clip_layout.get("x") or 0) + float(clip_layout.get("width") or 280)
    _assert_nodes_do_not_overlap(restored["nodes"])


def test_preserve_expanded_shot_nodes_rejects_stale_bootstrap_save() -> None:
    graph = build_bootstrap_graph(project_id="proj_keep01", prompt="keep shots")
    expanded = expand_clip_nodes_for_shots(graph, 3)
    stale = build_bootstrap_graph(project_id="proj_keep01", prompt="keep shots")
    stale["graph_id"] = expanded["graph_id"]
    kept = preserve_expanded_shot_nodes(stale, expanded)
    assert [node["id"] for node in kept["nodes"] if node_pipeline(node) == NODE_ROLE_CLIP] == [
        "n_clip_1",
        "n_clip_2",
        "n_clip_3",
    ]


def test_graph_store_does_not_shrink_expanded_shot_nodes(designer_store: DesignerGraphStore) -> None:
    graph = designer_store.save_graph(
        expand_clip_nodes_for_shots(
            build_bootstrap_graph(project_id="proj_keep02", prompt="keep shots"),
            3,
        )
    )
    stale = build_bootstrap_graph(project_id="proj_keep02", prompt="keep shots")
    stale["graph_id"] = graph["graph_id"]
    saved = designer_store.save_graph(stale)
    assert [node["id"] for node in saved["nodes"] if node_pipeline(node) == NODE_ROLE_CLIP] == [
        "n_clip_1",
        "n_clip_2",
        "n_clip_3",
    ]


def test_preserve_expanded_shot_nodes_keeps_user_deleted_extra_shots() -> None:
    graph = build_bootstrap_graph(project_id="proj_del01", prompt="delete extra shots")
    expanded = expand_clip_nodes_for_shots(graph, 4)
    drop = {"n_clip_4"}
    incoming = dict(expanded)
    incoming["nodes"] = [node for node in expanded["nodes"] if node["id"] not in drop]
    incoming["edges"] = [
        edge
        for edge in expanded["edges"]
        if edge.get("source") not in drop and edge.get("target") not in drop
    ]
    kept = preserve_expanded_shot_nodes(incoming, expanded)
    ids = {node["id"] for node in kept["nodes"]}
    assert "n_clip_4" not in ids
    assert {"n_clip_1", "n_clip_2", "n_clip_3"} <= ids


def test_graph_store_keeps_user_deleted_extra_shots(designer_store: DesignerGraphStore) -> None:
    graph = designer_store.save_graph(
        expand_clip_nodes_for_shots(
            build_bootstrap_graph(project_id="proj_del02", prompt="delete extra shots"),
            4,
        )
    )
    drop = {"n_clip_4"}
    incoming = dict(graph)
    incoming["nodes"] = [node for node in graph["nodes"] if node["id"] not in drop]
    incoming["edges"] = [
        edge
        for edge in graph["edges"]
        if edge.get("source") not in drop and edge.get("target") not in drop
    ]
    saved = designer_store.save_graph(incoming)
    ids = {node["id"] for node in saved["nodes"]}
    assert "n_clip_4" not in ids
    assert "n_clip_3" in ids


def test_preserve_skips_graft_when_user_marks_topology_edit() -> None:
    graph = build_bootstrap_graph(project_id="proj_del03", prompt="delete extra shots")
    expanded = expand_clip_nodes_for_shots(graph, 4)
    drop = {"n_clip_4"}
    incoming = dict(expanded)
    incoming["nodes"] = [node for node in expanded["nodes"] if node["id"] not in drop]
    incoming["edges"] = [
        edge
        for edge in expanded["edges"]
        if edge.get("source") not in drop and edge.get("target") not in drop
    ]
    incoming["metadata"] = {**(expanded.get("metadata") or {}), "user_topology_edit": True}
    kept = preserve_expanded_shot_nodes(incoming, expanded)
    ids = {node["id"] for node in kept["nodes"]}
    assert "n_clip_4" not in ids


def test_expand_shots_keeps_director_topology_and_syncs_prompts(
    designer_store: DesignerGraphStore, tmp_path: Path
) -> None:
    from jiuwenswarm.server.runtime.designer.executor import GraphExecutor

    graph = designer_store.save_graph(
        _handler_graph(build_bootstrap_graph(project_id="proj_split01", prompt="two shots")),
    )
    story = tmp_path / "two-shots.md"
    story.write_text(
        "# Storyboard\n\n"
        "## Storyboard\n\n"
        "| Shot | Timeline | Camera | Move | Character action | Continuity | Comment |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        "| 1 | 0.0-4.0s | wide | hold | enter | hold | shot one |\n"
        "| 2 | 4.0-8.0s | medium | push | walk | hold | shot two |\n",
        encoding="utf-8",
    )
    executor = GraphExecutor(designer_store)
    run = executor.create_run(graph)
    run["node_states"]["n_brief"] = {"status": NODE_STATUS_COMPLETED}
    run["node_states"]["n_character"] = {"status": NODE_STATUS_COMPLETED}
    run["node_states"]["n_scene"] = {"status": NODE_STATUS_COMPLETED}
    run["node_states"]["n_storyboard"] = {
        "status": NODE_STATUS_COMPLETED,
        "output_ref": {
            "kind": "table",
            "uri": story.resolve().as_uri(),
            "mime_type": "text/markdown",
        },
    }
    designer_store.save_run(run)
    expanded, remaining, _, _ = executor._expand_shots_if_needed(
        graph, run, {"n_clip_1", "n_compose"}, on_update=None
    )
    clip_ids = [node["id"] for node in expanded["nodes"] if node_pipeline(node) == NODE_ROLE_CLIP]
    assert clip_ids == ["n_clip_1"]
    assert not any(node_pipeline(node) == NODE_ROLE_FRAME for node in expanded["nodes"])
    assert (expanded.get("metadata") or {}).get("freeze_shot_topology") is True
    assert remaining == {"n_clip_1", "n_compose"}
    clip = next(node for node in expanded["nodes"] if node["id"] == "n_clip_1")
    assert ((clip.get("config") or {}).get("generate") or {}) == {
        "prompt": "shot one",
        "prompt_origin": "storyboard",
    }


def test_normalize_wires_existing_scene_on_old_bootstrap() -> None:
    graph = build_bootstrap_graph(project_id="proj_old02", prompt="legacy-align")
    if not any(node.get("id") == "n_scene" for node in graph["nodes"]):
        graph["nodes"].append(
            {
                "id": "n_scene",
                "type": NODE_TYPE_IMAGE,
                "label": "Scene",
                "config": {"role": NODE_ROLE_SCENE, "inputs": ["n_brief"]},
                "layout": {"x": 400, "y": 240, "width": 280, "height": 160},
            }
        )
    restored = normalize_execution_graph(graph)
    assert any(
        edge.get("source") == "n_scene"
        and edge.get("target") == "n_storyboard"
        and edge.get("kind") == EDGE_KIND_SYNC
        for edge in restored["edges"]
    )
    assert any(edge.get("source") == "n_brief" and edge.get("target") == "n_scene" for edge in restored["edges"])


def test_graph_store_list_by_project(designer_store: DesignerGraphStore) -> None:
    first = designer_store.save_graph(
        build_bootstrap_graph(project_id="proj_list_a", prompt="first"),
    )
    second = designer_store.save_graph(
        build_bootstrap_graph(project_id="proj_list_a", prompt="second"),
    )
    designer_store.save_graph(
        build_bootstrap_graph(project_id="proj_list_b", prompt="other project"),
    )
    graphs = designer_store.list_graphs_for_project("proj_list_a")
    graph_ids = {graph["graph_id"] for graph in graphs}
    assert graph_ids == {first["graph_id"], second["graph_id"]}
    all_ids = {graph["graph_id"] for graph in designer_store.list_graphs()}
    assert first["graph_id"] in all_ids
    assert second["graph_id"] in all_ids


@pytest.mark.asyncio
async def test_mock_executor_completes_run(
    designer_store: DesignerGraphStore, stub_clip_video: None
) -> None:
    graph = designer_store.save_graph(
        _handler_graph(build_bootstrap_graph(project_id="proj_exec01", prompt="execute me")),
    )
    executor = GraphExecutor(designer_store)
    run = executor.create_run(graph)
    started = await executor.start_run(run["run_id"])
    assert started["status"] == RUN_STATUS_RUNNING
    task = executor._tasks.get(run["run_id"])
    if task is not None:
        await task
    finished = designer_store.get_run(run["run_id"])
    assert finished is not None
    assert finished["status"] == RUN_STATUS_COMPLETED
    node_states = finished["node_states"]
    assert all(
        state.get("status") == NODE_STATUS_COMPLETED for state in node_states.values()
    )
    character = node_states["n_character"]
    storyboard = node_states["n_storyboard"]
    scene = node_states["n_scene"]
    assert str(character.get("output_ref", {}).get("uri") or "").startswith("file:")
    assert str(storyboard.get("output_ref", {}).get("uri") or "").startswith("file:")
    assert scene.get("output_ref", {}).get("kind") == NODE_TYPE_IMAGE
    clip = node_states["n_clip_1"]
    assert clip.get("output_ref", {}).get("kind") == NODE_TYPE_VIDEO
    assert str(clip.get("output_ref", {}).get("label") or "").endswith(".mp4")
    compose = node_states["n_compose"]
    assert compose.get("output_ref", {}).get("kind") == NODE_TYPE_VIDEO
    assert str(compose.get("output_ref", {}).get("label") or "").endswith(".mp4")
    assert (character.get("started_at") or 0) <= (clip.get("started_at") or 0)
    assert (storyboard.get("started_at") or 0) <= (clip.get("started_at") or 0)
    assert (scene.get("completed_at") or 0) <= (clip.get("started_at") or 0)
    assert (clip.get("completed_at") or 0) <= (compose.get("started_at") or 0)
    saved_graph = designer_store.get_graph(graph["graph_id"])
    assert saved_graph is not None
    assert [node["id"] for node in saved_graph["nodes"] if node_pipeline(node) == NODE_ROLE_CLIP] == [
        "n_clip_1",
    ]
    assert not any(
        node_pipeline(node) == NODE_ROLE_FRAME for node in saved_graph["nodes"]
    )


def test_normalize_drops_legacy_keyframe_chain() -> None:
    graph = expand_clip_nodes_for_shots(
        build_bootstrap_graph(project_id="proj_chain01", prompt="drop chain"),
        2,
    )
    for index in (1, 2):
        graph["nodes"].append(
            {
                "id": f"n_frame_{index}",
                "type": NODE_TYPE_IMAGE,
                "label": f"keyframe {index}",
                "config": {
                    "role": NODE_TYPE_IMAGE,
                    "pipeline": NODE_ROLE_FRAME,
                    "shot_index": index,
                    "inputs": ["n_character", "n_scene"] + (["n_frame_1"] if index == 2 else []),
                },
            }
        )
    graph["edges"].append(
        {
            "id": "e_n_frame_1_n_frame_2",
            "source": "n_frame_1",
            "target": "n_frame_2",
            "kind": "data",
        }
    )
    restored = normalize_execution_graph(graph)
    assert not any(
        edge.get("source") == "n_frame_1" and edge.get("target") == "n_frame_2"
        for edge in restored["edges"]
    )
    restored_frame2 = next(node for node in restored["nodes"] if node["id"] == "n_frame_2")
    assert "n_frame_1" not in ((restored_frame2.get("config") or {}).get("inputs") or [])


@pytest.mark.asyncio
async def test_running_status_is_saved_before_handler_returns(
    designer_store: DesignerGraphStore,
    stub_clip_video: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.server.runtime.designer.handlers.text_nodes import BriefNodeHandler

    gate = asyncio.Event()
    seen: dict[str, str] = {}
    original = BriefNodeHandler.execute

    async def paused(self, node, ctx):
        run = designer_store.get_run(ctx.run_id)
        seen["status"] = str((run.get("node_states") or {}).get(node["id"], {}).get("status") or "")
        await gate.wait()
        return await original(self, node, ctx)

    monkeypatch.setattr(BriefNodeHandler, "execute", paused)
    graph = designer_store.save_graph(
        _handler_graph(build_bootstrap_graph(project_id="proj_run_persist", prompt="persist running")),
    )
    executor = GraphExecutor(designer_store)
    run = executor.create_run(graph)
    await executor.start_run(run["run_id"])
    task = executor._tasks.get(run["run_id"])
    try:
        for _ in range(50):
            if "status" in seen:
                break
            await asyncio.sleep(0.02)
        assert seen.get("status") == NODE_STATUS_RUNNING
    finally:
        gate.set()
        if task is not None:
            await task


def test_create_scoped_run_for_node_covers_every_named_node(
    designer_store: DesignerGraphStore,
) -> None:
    """A plan naming several nodes must get all their chains, not just the first.

    Regression: a confirmation turn that asked for the character sheet AND the
    scene set built only the first id's ancestor chain, while the reply announced
    both stages — the scene stayed ungenerated until the user asked again.
    """
    graph = designer_store.save_graph(
        _handler_graph(build_bootstrap_graph(project_id="proj_scoped_multi", prompt="multi")),
    )
    executor = GraphExecutor(designer_store)
    ids = [str(node.get("id") or "") for node in graph["nodes"]]
    character = next(item for item in ids if "character" in item)
    scene = next(item for item in ids if "scene" in item)

    single = executor.create_scoped_run_for_node(graph, node_id=character)
    single_scope = list(single["metadata"]["scope_node_ids"])
    assert scene not in single_scope, single_scope

    multi = executor.create_scoped_run_for_node(
        graph, node_id=character, node_ids=[character, scene]
    )
    multi_scope = list(multi["metadata"]["scope_node_ids"])
    assert character in multi_scope
    assert scene in multi_scope, multi_scope
    # The run only carries the nodes it will actually build.
    assert set(multi["node_states"]) == set(multi_scope)


def test_create_rerun_parks_orphaned_running_and_marks_single_node(
    designer_store: DesignerGraphStore,
) -> None:
    graph = designer_store.save_graph(
        _handler_graph(build_bootstrap_graph(project_id="proj_park_running", prompt="park")),
    )
    executor = GraphExecutor(designer_store)
    source = executor.create_run(graph)
    for state in source["node_states"].values():
        state["status"] = NODE_STATUS_COMPLETED
    source["node_states"]["n_scene"] = {
        "status": NODE_STATUS_RUNNING,
        "activity": {"kind": "tool_call", "text": "calling call_image_model"},
        "error": None,
    }
    source["node_states"]["n_character"] = {"status": "failed", "error": "image_gen failed"}
    designer_store.save_run(source)
    rerun = executor.create_rerun(graph, source_run=source, node_id="n_character")
    assert rerun["metadata"]["target_node_id"] == "n_character"
    assert rerun["node_states"]["n_character"]["status"] == "pending"
    assert not rerun["node_states"]["n_character"].get("error")
    assert rerun["node_states"]["n_scene"]["status"] == "pending"
    assert "activity" not in rerun["node_states"]["n_scene"]


def test_park_running_nodes_leaves_failed_and_completed(
    designer_store: DesignerGraphStore,
) -> None:
    graph = designer_store.save_graph(
        _handler_graph(build_bootstrap_graph(project_id="proj_park_wave", prompt="park wave")),
    )
    executor = GraphExecutor(designer_store)
    run = executor.create_run(graph)
    run["node_states"]["n_character"] = {"status": "failed", "error": "no image"}
    run["node_states"]["n_scene"] = {"status": NODE_STATUS_RUNNING, "error": None}
    run["node_states"]["n_brief"] = {"status": NODE_STATUS_COMPLETED}
    executor._park_running_nodes(run, None)
    assert run["node_states"]["n_character"]["status"] == "failed"
    assert run["node_states"]["n_scene"]["status"] == "pending"
    assert run["node_states"]["n_brief"]["status"] == NODE_STATUS_COMPLETED


@pytest.mark.asyncio
async def test_rerun_single_node_keeps_upstream_outputs(
    designer_store: DesignerGraphStore, stub_clip_video: None
) -> None:
    graph = designer_store.save_graph(
        _handler_graph(build_bootstrap_graph(project_id="proj_rerun01", prompt="rerun clip")),
    )
    executor = GraphExecutor(designer_store)
    first = executor.create_run(graph)
    await executor.start_run(first["run_id"])
    task = executor._tasks.get(first["run_id"])
    if task is not None:
        await task
    finished = designer_store.get_run(first["run_id"])
    assert finished is not None
    brief_started = finished["node_states"]["n_brief"].get("started_at")
    brief_uri = (finished["node_states"]["n_brief"].get("output_ref") or {}).get("uri")

    rerun = executor.create_rerun(graph, source_run=finished, node_id="n_clip_1")
    assert rerun["node_states"]["n_clip_1"]["status"] == "pending"
    assert rerun["node_states"]["n_brief"]["status"] == NODE_STATUS_COMPLETED
    await executor.start_run(rerun["run_id"])
    worker = executor._tasks.get(rerun["run_id"])
    if worker is not None:
        await worker
    again = designer_store.get_run(rerun["run_id"])
    assert again is not None
    assert again["status"] == RUN_STATUS_COMPLETED
    assert again["node_states"]["n_brief"].get("started_at") == brief_started
    assert (again["node_states"]["n_brief"].get("output_ref") or {}).get("uri") == brief_uri
    assert again["node_states"]["n_clip_1"]["status"] == NODE_STATUS_COMPLETED
    original_clip = (finished["node_states"]["n_clip_1"].get("output_ref") or {}).get("uri")
    new_clip = (again["node_states"]["n_clip_1"].get("output_ref") or {}).get("uri")
    assert new_clip
    assert new_clip != original_clip
    assert not (again["node_states"]["n_clip_1"].get("candidate_output_ref") or {}).get("uri")

    with pytest.raises(ValueError, match="upstream not ready"):
        unfinished = {
            **finished,
            "node_states": {
                **finished["node_states"],
                "n_storyboard": {"status": "pending"},
            },
        }
        executor.create_rerun(graph, source_run=unfinished, node_id="n_clip_1")


def _chained_clip_graph(designer_store: DesignerGraphStore, project_id: str):
    """Bootstrap graph plus a second clip that continues the first.

    Mirrors what the leader wires for chained shots: clip 2 takes clip 1's tail
    frame, so clip 1 is clip 2's upstream.
    """
    base = build_bootstrap_graph(project_id=project_id, prompt="chain")
    clip_1 = next(node for node in base["nodes"] if node["id"] == "n_clip_1")
    clip_2 = {
        **clip_1,
        "id": "n_clip_2",
        "label": "Clip 2",
        "config": dict(clip_1.get("config") or {}),
    }
    return designer_store.save_graph(
        _handler_graph(
            {
                **base,
                "nodes": [*base["nodes"], clip_2],
                "edges": [
                    *base["edges"],
                    {"id": "e_clip_1_clip_2", "source": "n_clip_1", "target": "n_clip_2"},
                ],
            }
        )
    )


def test_rerun_allows_targets_that_depend_on_each_other(
    designer_store: DesignerGraphStore,
) -> None:
    """Asking for chained clips together must not be refused.

    ``create_rerun`` required every predecessor to be completed already,
    including predecessors that are themselves targets of the same rerun — so
    "生成镜头视频" on clips chained first-frame-to-first-frame raised
    "upstream not ready: n_clip_1", the very node the run would have rebuilt.
    Nothing ran, and the reply still said generation had started.
    """
    graph = _chained_clip_graph(designer_store, "proj_chain")
    executor = GraphExecutor(designer_store)
    source = executor.create_run(graph)
    for state in source["node_states"].values():
        state["status"] = NODE_STATUS_COMPLETED
    # The earlier clip failed earlier in the session; this rerun rebuilds it.
    source["node_states"]["n_clip_1"] = {"status": "failed", "error": "[ERROR]: boom"}
    designer_store.save_run(source)

    rerun = executor.create_rerun(
        graph, source_run=source, node_id="n_clip_1", node_ids=["n_clip_1", "n_clip_2"]
    )

    assert rerun["node_states"]["n_clip_1"]["status"] == "pending"
    assert rerun["node_states"]["n_clip_2"]["status"] == "pending"


def test_rerun_still_refuses_an_unfinished_upstream_it_will_not_build(
    designer_store: DesignerGraphStore,
) -> None:
    """Widening must not drop the check for a predecessor outside the target set."""
    graph = _chained_clip_graph(designer_store, "proj_chain_guard")
    executor = GraphExecutor(designer_store)
    source = executor.create_run(graph)
    for state in source["node_states"].values():
        state["status"] = NODE_STATUS_COMPLETED
    source["node_states"]["n_clip_1"] = {"status": "failed", "error": "boom"}
    designer_store.save_run(source)

    # Only clip 2 is rebuilt here, so its failed upstream really is not ready.
    with pytest.raises(ValueError, match="upstream not ready: n_clip_1"):
        executor.create_rerun(graph, source_run=source, node_id="n_clip_2", node_ids=["n_clip_2"])


async def _drain_run(executor: GraphExecutor, run_id: str) -> None:
    """Let the run's task finish; its own outcome is not what these tests assert."""
    task = executor._tasks.get(run_id)
    if task is None:
        return
    try:
        await task
    except Exception:  # noqa: BLE001 - cleanup runs in the task's finally either way
        pass


@pytest.mark.asyncio
async def test_cleanup_run_drops_the_handoff_wake_event(
    designer_store: DesignerGraphStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One asyncio.Event per run must not outlive the run.

    ``_cleanup_run`` dropped every other per-run dict but this one.
    ``_get_handoff_wake`` creates the entry on demand — the wave scheduler calls
    it for each run — so a long-lived AgentServer kept one Event alive per run it
    had ever executed.
    """
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.model_tools.require_llm", lambda: None
    )
    graph = designer_store.save_graph(
        _handler_graph(build_bootstrap_graph(project_id="proj_wake", prompt="wake")),
    )
    executor = GraphExecutor(designer_store)
    run_id = executor.create_run(graph)["run_id"]
    baseline = len(executor._handoff_wake)

    executor._get_handoff_wake(run_id)
    assert run_id in executor._handoff_wake

    await executor.start_run(run_id)
    await _drain_run(executor, run_id)

    assert run_id not in executor._handoff_wake
    assert len(executor._handoff_wake) == baseline


@pytest.mark.asyncio
async def test_repeated_runs_leave_no_handoff_wake_entries(
    designer_store: DesignerGraphStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Creating and cleaning up several runs returns the map to its start size."""
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.model_tools.require_llm", lambda: None
    )
    graph = designer_store.save_graph(
        _handler_graph(build_bootstrap_graph(project_id="proj_wake_many", prompt="wake many")),
    )
    executor = GraphExecutor(designer_store)
    baseline = len(executor._handoff_wake)

    for _ in range(3):
        run_id = executor.create_run(graph)["run_id"]
        executor._get_handoff_wake(run_id)
        await executor.start_run(run_id)
        await _drain_run(executor, run_id)

    assert executor._handoff_wake == {}
    assert len(executor._handoff_wake) == baseline


@pytest.mark.asyncio
async def test_has_active_tasks_tracks_live_runs_of_one_graph(
    designer_store: DesignerGraphStore,
) -> None:
    """The chat guard needs per-graph liveness, and cleanup must clear it.

    Two overlapping runs on one graph would share the graph and its node_states,
    so the chat path refuses a second turn while one is still generating.
    """
    graph = designer_store.save_graph(
        _handler_graph(build_bootstrap_graph(project_id="proj_active", prompt="active")),
    )
    executor = GraphExecutor(designer_store)
    run_id = executor.create_run(graph)["run_id"]

    assert executor.has_active_tasks(graph["graph_id"]) is False

    pending: asyncio.Future = asyncio.get_running_loop().create_future()
    executor._tasks[run_id] = pending
    executor._live_runs[run_id] = {"run_id": run_id, "graph_id": graph["graph_id"]}

    assert executor.has_active_tasks(graph["graph_id"]) is True
    assert executor.has_active_tasks("graph_somewhere_else") is False

    # A finished run is not an active one, even before its cleanup runs.
    pending.set_result(None)
    assert executor.has_active_tasks(graph["graph_id"]) is False

    executor._cleanup_run(run_id)
    assert executor.has_active_tasks(graph["graph_id"]) is False


@pytest.mark.asyncio
async def test_continue_after_failure_retries_failed_node(
    designer_store: DesignerGraphStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.model_tools.require_llm", lambda: None
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.model_tools.require_media_models",
        lambda **_: None,
    )
    graph = designer_store.save_graph(
        _handler_graph(build_bootstrap_graph(project_id="proj_continue_failed", prompt="continue")),
    )
    executor = GraphExecutor(designer_store)
    scheduled: dict[str, dict] = {}

    async def capture(graph, run, *, on_update):
        scheduled.update(deepcopy(run["node_states"]))

    monkeypatch.setattr(executor, "_execute_run", capture)
    run = executor.create_run(graph)
    for state in run["node_states"].values():
        state["status"] = NODE_STATUS_COMPLETED
    run["status"] = "failed"
    run["error"] = "insufficient credit"
    run["node_states"]["n_clip_1"] = {
        "status": "failed",
        "error": "insufficient credit",
        "output_ref": None,
    }
    run["node_states"]["n_clip_2"] = {"status": "pending"}
    designer_store.save_run(run)

    await executor.start_run(run["run_id"])
    await executor._tasks[run["run_id"]]

    assert scheduled["n_clip_1"]["status"] == "pending"
    assert not scheduled["n_clip_1"].get("error")
    assert scheduled["n_clip_2"]["status"] == "pending"
    assert scheduled["n_brief"]["status"] == NODE_STATUS_COMPLETED


@pytest.mark.asyncio
async def test_rerun_compose_replaces_film_in_place(
    designer_store: DesignerGraphStore, stub_clip_video: None
) -> None:
    graph = designer_store.save_graph(
        _handler_graph(build_bootstrap_graph(project_id="proj_compose_rerun", prompt="rerun film")),
    )
    executor = GraphExecutor(designer_store)
    first = executor.create_run(graph)
    await executor.start_run(first["run_id"])
    task = executor._tasks.get(first["run_id"])
    if task is not None:
        await task
    finished = designer_store.get_run(first["run_id"])
    assert finished is not None
    original = (finished["node_states"]["n_compose"].get("output_ref") or {}).get("uri")
    assert original

    rerun = executor.create_rerun(graph, source_run=finished, node_id="n_compose")
    assert rerun["node_states"]["n_compose"]["status"] == "pending"
    assert not (rerun["node_states"]["n_compose"].get("output_ref") or {}).get("uri")
    await executor.start_run(rerun["run_id"])
    worker = executor._tasks.get(rerun["run_id"])
    if worker is not None:
        await worker
    again = designer_store.get_run(rerun["run_id"])
    assert again is not None
    assert again["status"] == RUN_STATUS_COMPLETED
    replaced = (again["node_states"]["n_compose"].get("output_ref") or {}).get("uri")
    assert replaced
    assert replaced != original
    assert not (again["node_states"]["n_compose"].get("candidate_output_ref") or {}).get("uri")


@pytest.mark.asyncio
async def test_rerun_promotes_generated_image_over_fallback_notes(
    designer_store: DesignerGraphStore, stub_clip_video: None, tmp_path: Path
) -> None:
    graph = designer_store.save_graph(
        _handler_graph(build_bootstrap_graph(project_id="proj_notes01", prompt="promote png")),
    )
    executor = GraphExecutor(designer_store)
    first = executor.create_run(graph)
    await executor.start_run(first["run_id"])
    task = executor._tasks.get(first["run_id"])
    if task is not None:
        await task
    finished = designer_store.get_run(first["run_id"])
    assert finished is not None
    notes = tmp_path / "designer_character_fallback.md"
    notes.write_text("fallback character notes\n", encoding="utf-8")
    notes_ref = {
        "kind": "text",
        "uri": notes.resolve().as_uri(),
        "mime_type": "text/markdown",
        "label": notes.name,
    }
    finished["node_states"]["n_character"]["output_ref"] = notes_ref
    finished["node_states"]["n_character"]["output_refs"] = [notes_ref]
    designer_store.save_run(finished)

    rerun = executor.create_rerun(graph, source_run=finished, node_id="n_character")
    assert not (rerun["node_states"]["n_character"].get("output_ref") or {}).get("uri")
    await executor.start_run(rerun["run_id"])
    worker = executor._tasks.get(rerun["run_id"])
    if worker is not None:
        await worker
    again = designer_store.get_run(rerun["run_id"])
    assert again is not None
    character = again["node_states"]["n_character"]
    assert character["status"] == NODE_STATUS_COMPLETED
    assert (character.get("output_ref") or {}).get("kind") == NODE_TYPE_IMAGE
    assert str((character.get("output_ref") or {}).get("uri") or "").endswith(".png")
    assert not (character.get("candidate_output_ref") or {}).get("uri")


def test_bootstrap_treats_default_project_as_create(designer_store: DesignerGraphStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from jiuwenswarm.server.runtime.gateway_adapter import designer_adapter as adapter

    monkeypatch.setattr(adapter, "_store", designer_store)
    monkeypatch.setattr(adapter, "resolve_request_work_mode", lambda params, channel: ("work", None))
    monkeypatch.setattr(
        adapter.project_store,
        "resolve_default_project_dir",
        lambda name, mode: str(tmp_path / "designer-project"),
    )
    created: list[tuple[str, str, str]] = []

    def fake_create(name: str, project_dir: str, work_mode: str):
        created.append((name, project_dir, work_mode))
        return (
            SimpleNamespace(
                project_id="proj_created01",
                project_dir=project_dir,
                work_mode=work_mode,
                hidden=False,
            ),
            False,
        )

    monkeypatch.setattr(adapter.project_store, "create_project_checked", fake_create)

    analysis = {
        "source": "llm",
        "characters": [{"id": "char_1", "name": "Traveler", "description": "coat"}],
        "scenes": [{"id": "set_1", "name": "Station", "description": "platform"}],
        "shots": [
            {
                "shot_index": 1,
                "action": "walks onto the platform",
                "camera": "medium",
                "on_screen": ["char_1"],
                "setting_id": "set_1",
                "timeline": "0-5s",
            }
        ],
        "target_shot_count": 1,
    }
    payload, error, code = adapter._bootstrap_graph(
        {"prompt": "火车站短片", "project_id": "default"},
        "web",
        analysis,
    )
    assert error is None
    assert code is None
    assert payload is not None
    assert payload["project_id"] == "proj_created01"
    assert payload["graph"]["title"] == "火车站短片"
    assert created


def test_start_run_rejects_run_from_another_graph(
    designer_store: DesignerGraphStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiuwenswarm.server.runtime.designer.executor import GraphExecutor
    from jiuwenswarm.server.runtime.gateway_adapter import designer_adapter as adapter

    first = designer_store.save_graph(
        _handler_graph(build_bootstrap_graph(project_id="proj_iso_a", prompt="scheme a")),
    )
    second = designer_store.save_graph(
        _handler_graph(build_bootstrap_graph(project_id="proj_iso_b", prompt="scheme b")),
    )
    executor = GraphExecutor(designer_store)
    run = executor.create_run(first)
    monkeypatch.setattr(adapter, "_store", designer_store)
    monkeypatch.setattr(adapter, "_executor", executor)
    payload, error, code = adapter._start_run(
        {"graph_id": second["graph_id"], "run_id": run["run_id"], "node_id": "n_brief"}
    )
    assert payload is None
    assert code == "BAD_REQUEST"
    assert error == "run does not belong to graph"


def test_list_graphs_includes_video_summary(
    designer_store: DesignerGraphStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiuwenswarm.server.runtime.gateway_adapter import designer_adapter as adapter

    monkeypatch.setattr(adapter, "_store", designer_store)
    graph = designer_store.save_graph(
        build_bootstrap_graph(project_id="proj_sum01", prompt="clip ready"),
    )
    designer_store.save_run(
        {
            "schema_version": "designer-execution-run.v1",
            "run_id": "run_sum01",
            "graph_id": graph["graph_id"],
            "project_id": "proj_sum01",
            "status": RUN_STATUS_COMPLETED,
            "node_states": {
                "n_clip": {
                    "status": NODE_STATUS_COMPLETED,
                    "output_ref": {
                        "kind": NODE_TYPE_VIDEO,
                        "uri": (tmp_path / "generated_clip.mp4").resolve().as_uri(),
                        "label": "generated_clip.mp4",
                    },
                }
            },
            "current_node_ids": [],
        }
    )
    payload, error, code = adapter._list_graphs({})
    assert error is None
    assert code is None
    assert payload is not None
    summary = next(item for item in payload["summaries"] if item["graph_id"] == graph["graph_id"])
    assert summary["has_video"] is True
    assert summary["clip_label"] == "generated_clip.mp4"
    listed = next(item for item in payload["graphs"] if item["graph_id"] == graph["graph_id"])
    assert "nodes" not in listed
    assert "edges" not in listed


def test_list_graphs_omits_graph_bodies(
    designer_store: DesignerGraphStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiuwenswarm.server.runtime.gateway_adapter import designer_adapter as adapter

    monkeypatch.setattr(adapter, "_store", designer_store)
    graph = designer_store.save_graph(
        build_bootstrap_graph(project_id="proj_slim01", prompt="slim listing"),
    )

    payload, error, code = adapter._list_graphs({})

    assert error is None
    assert code is None
    assert payload is not None
    listed = next(item for item in payload["graphs"] if item["graph_id"] == graph["graph_id"])
    assert set(listed) <= {"graph_id", "project_id", "title", "updated_at", "schema_version"}
    assert "nodes" not in listed
    assert "edges" not in listed
    assert graph["graph_id"] in {item["graph_id"] for item in payload["summaries"]}


def test_get_graph_hydrates_node_outputs_from_latest_run(
    designer_store: DesignerGraphStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiuwenswarm.server.runtime.gateway_adapter import designer_adapter as adapter

    monkeypatch.setattr(adapter, "_store", designer_store)
    monkeypatch.setattr(adapter._executor, "_store", designer_store)
    graph = designer_store.save_graph(
        build_bootstrap_graph(project_id="proj_hydrate01", prompt="train arrives"),
    )
    brief_uri = (tmp_path / "brief.md").resolve().as_uri()
    designer_store.save_run(
        {
            "schema_version": "designer-execution-run.v1",
            "run_id": "run_hydrate01",
            "graph_id": graph["graph_id"],
            "project_id": "proj_hydrate01",
            "status": RUN_STATUS_COMPLETED,
            "node_states": {
                "n_brief": {
                    "status": NODE_STATUS_COMPLETED,
                    "output_ref": {
                        "kind": NODE_TYPE_TEXT,
                        "uri": brief_uri,
                        "label": "brief.md",
                    },
                }
            },
            "current_node_ids": [],
        }
    )
    payload, error, code = adapter._get_graph({"graph_id": graph["graph_id"]})
    assert error is None
    assert code is None
    assert payload is not None
    brief = next(node for node in payload["graph"]["nodes"] if node["id"] == "n_brief")
    assert brief["output_ref"]["uri"] == brief_uri


@pytest.mark.asyncio
async def test_sync_peers_start_together_and_block_downstream(
    designer_store: DesignerGraphStore,
    monkeypatch: pytest.MonkeyPatch,
    stub_clip_video: None,
) -> None:
    import time

    from jiuwenswarm.server.runtime.designer import executor as executor_mod
    from jiuwenswarm.server.runtime.designer.handlers import NODE_HANDLERS, RoleNodeHandler

    monkeypatch.setattr(executor_mod, "_MOCK_NODE_DELAY_SECONDS", 0.05)
    starts: dict[str, float] = {}
    originals = {
        NODE_ROLE_CHARACTER_DESIGN: NODE_HANDLERS[NODE_ROLE_CHARACTER_DESIGN],
        NODE_ROLE_STORYBOARD: NODE_HANDLERS[NODE_ROLE_STORYBOARD],
        NODE_ROLE_CLIP: NODE_HANDLERS[NODE_ROLE_CLIP],
    }

    class TimedHandler(RoleNodeHandler):
        async def execute(self, node, ctx):
            starts[node["id"]] = time.monotonic()
            return await originals[self.role].execute(node, ctx)

    monkeypatch.setitem(NODE_HANDLERS, NODE_ROLE_CHARACTER_DESIGN, TimedHandler(NODE_ROLE_CHARACTER_DESIGN))
    monkeypatch.setitem(NODE_HANDLERS, NODE_ROLE_STORYBOARD, TimedHandler(NODE_ROLE_STORYBOARD))
    monkeypatch.setitem(NODE_HANDLERS, NODE_ROLE_CLIP, TimedHandler(NODE_ROLE_CLIP))

    graph = designer_store.save_graph(
        _handler_graph(build_bootstrap_graph(project_id="proj_sync01", prompt="align peers")),
    )
    execu = GraphExecutor(designer_store)
    run = execu.create_run(graph)
    events = [event async for event in execu.run(graph, run["run_id"])]
    assert events
    assert any(event.event == "designer.run.updated" for event in events)
    finished = designer_store.get_run(run["run_id"])
    assert finished is not None
    assert finished["status"] == RUN_STATUS_COMPLETED
    assert abs(starts["n_character"] - starts["n_storyboard"]) < 0.04
    assert starts["n_clip_1"] > max(starts["n_character"], starts["n_storyboard"])


@pytest.mark.asyncio
async def test_sync_barrier_blocks_even_without_second_data_edge(
    designer_store: DesignerGraphStore,
    monkeypatch: pytest.MonkeyPatch,
    stub_clip_video: None,
) -> None:
    from jiuwenswarm.common.schema.designer_graph import (
        EDGE_KIND_DATA,
        EDGE_KIND_SYNC,
        NODE_ROLE_SCENE,
        NODE_TYPE_IMAGE,
        NODE_TYPE_TEXT,
        SCHEMA_VERSION,
        normalize_execution_graph,
    )
    from jiuwenswarm.server.runtime.designer import executor as executor_mod

    monkeypatch.setattr(executor_mod, "_MOCK_NODE_DELAY_SECONDS", 0)
    graph = designer_store.save_graph(
        _handler_graph(
        normalize_execution_graph(
            {
                "schema_version": SCHEMA_VERSION,
                "graph_id": "graph_barrier01",
                "project_id": "proj_barrier01",
                "title": "barrier",
                "source": "manual",
                "nodes": [
                    {"id": "a", "type": NODE_TYPE_TEXT, "label": "A", "config": {"role": "brief"}},
                    {
                        "id": "b",
                        "type": NODE_TYPE_IMAGE,
                        "label": "B",
                        "config": {"role": NODE_ROLE_CHARACTER_DESIGN},
                    },
                    {
                        "id": "s",
                        "type": NODE_TYPE_IMAGE,
                        "label": "S",
                        "config": {"role": NODE_ROLE_SCENE},
                    },
                    {
                        "id": "c",
                        "type": NODE_TYPE_TABLE,
                        "label": "C",
                        "config": {"role": NODE_ROLE_STORYBOARD},
                    },
                    {"id": "d", "type": NODE_TYPE_IMAGE, "label": "D", "config": {"role": "frame"}},
                ],
                "edges": [
                    {"id": "e1", "source": "a", "target": "b", "kind": EDGE_KIND_DATA},
                    {"id": "e2", "source": "a", "target": "c", "kind": EDGE_KIND_DATA},
                    {"id": "e5", "source": "a", "target": "s", "kind": EDGE_KIND_DATA},
                    {"id": "e3", "source": "b", "target": "c", "kind": EDGE_KIND_SYNC},
                    {"id": "e4", "source": "b", "target": "d", "kind": EDGE_KIND_DATA},
                    {"id": "e6", "source": "s", "target": "d", "kind": EDGE_KIND_DATA},
                ],
            }
        )
        )
    )
    execu = GraphExecutor(designer_store)
    run = execu.create_run(graph)
    await execu.start_run(run["run_id"])
    worker = execu._tasks.get(run["run_id"])
    if worker is not None:
        await worker
    finished = designer_store.get_run(run["run_id"])
    assert finished is not None
    assert finished["status"] == RUN_STATUS_COMPLETED
    assert (finished["node_states"]["c"].get("completed_at") or 0) <= (
        finished["node_states"]["d"].get("started_at") or 0
    )


def test_normalize_node_rejects_unknown_role() -> None:
    with pytest.raises(DesignerGraphValidationError, match="unsupported node role"):
        normalize_node(
            {
                "id": "n_x",
                "type": NODE_TYPE_TEXT,
                "label": "x",
                "config": {"role": "not_a_role"},
            }
        )


def test_normalize_node_accepts_typed_config() -> None:
    node = normalize_node(
        {
            "id": "n_brief",
            "type": NODE_TYPE_TEXT,
            "label": "brief",
            "config": {
                "role": "brief",
                "prompt": "晨间",
                "inputs": ["n_src"],
                "delegate": "handler",
                "generate": {"prompt": "站台", "aspect_ratio": "16:9"},
                "interaction_mode": "generate",
            },
        }
    )
    assert node["config"]["role"] == "text"
    assert node["config"]["pipeline"] == "brief"
    assert node["config"]["prompt"] == "晨间"
    assert node["config"]["inputs"] == ["n_src"]
    assert node["config"]["delegate"] == "handler"
    assert node["config"]["generate"]["prompt"] == "站台"
    assert node["config"]["interaction_mode"] == "generate"


def test_graph_prompt_reads_generate_prompt() -> None:
    node = normalize_node(
        {
            "id": "n_scene",
            "type": NODE_TYPE_IMAGE,
            "label": "场景",
            "config": {
                "role": "scene",
                "generate": {"prompt": "火车站晨间"},
            },
        }
    )
    graph = {
        "schema_version": "designer-execution-graph.v1",
        "graph_id": "g1",
        "project_id": "p1",
        "title": "标题",
        "nodes": [node],
        "edges": [],
    }
    assert graph_prompt(graph, node) == "火车站晨间"


def test_apply_graph_patch_upserts_and_removes() -> None:
    graph = build_bootstrap_graph(project_id="proj_patch01", prompt="patch me")
    patched = apply_graph_patch(
        graph,
        {
            "title": "改过的标题",
            "upsert_nodes": [
                {
                    "id": "n_extra",
                    "type": NODE_TYPE_TEXT,
                    "label": "备注",
                    "config": {"role": "brief", "prompt": "extra"},
                }
            ],
            "upsert_edges": [
                {
                    "id": "e_brief_extra",
                    "source": "n_brief",
                    "target": "n_extra",
                    "kind": "data",
                }
            ],
        },
    )
    assert patched["title"] == "改过的标题"
    assert any(node["id"] == "n_extra" for node in patched["nodes"])
    assert any(edge["id"] == "e_brief_extra" for edge in patched["edges"])
    removed = apply_graph_patch(
        patched,
        {"remove_node_ids": ["n_extra"], "remove_edge_ids": ["e_brief_extra"]},
    )
    assert all(node["id"] != "n_extra" for node in removed["nodes"])
    assert all(edge["id"] != "e_brief_extra" for edge in removed["edges"])


@pytest.mark.asyncio
async def test_executor_on_update_includes_node_id(
    designer_store: DesignerGraphStore, stub_clip_video: None
) -> None:
    events: list[str | None] = []

    def on_update(run, node_id=None):  # noqa: ANN001
        events.append(node_id)

    graph = designer_store.save_graph(
        _handler_graph(build_bootstrap_graph(project_id="proj_evt01", prompt="events")),
    )
    executor = GraphExecutor(designer_store)
    run = executor.create_run(graph)
    await executor.start_run(run["run_id"], on_update=on_update)
    task = executor._tasks.get(run["run_id"])
    if task is not None:
        await task
    assert "n_brief" in events
    assert None in events


@pytest.mark.asyncio
async def test_subagent_delegate_uses_registered_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiuwenswarm.server.runtime.designer.handlers.text_nodes import BriefNodeHandler
    from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext
    from jiuwenswarm.server.runtime.designer.subagent import register_designer_subagent_runner

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.common.get_agent_workspace_dir",
        lambda: tmp_path,
    )

    async def runner(prompt: str) -> str:
        return f"FROM_SUBAGENT:{prompt[:12]}"

    register_designer_subagent_runner(runner)
    try:
        graph = normalize_execution_graph(
            {
                "schema_version": "designer-execution-graph.v1",
                "graph_id": "graph_sub01",
                "project_id": "proj_sub01",
                "title": "sub",
                "description": "x",
                "source": "manual",
                "nodes": [
                    {
                        "id": "n_brief",
                        "type": NODE_TYPE_TEXT,
                        "label": "brief",
                        "config": {
                            "role": "brief",
                            "prompt": "火车站",
                            "delegate": "subagent",
                        },
                    }
                ],
                "edges": [],
            }
        )
        result = await BriefNodeHandler().execute(
            graph["nodes"][0],
            NodeExecutionContext(graph=graph, run_id="run_sub01", node_id="n_brief"),
        )
        assert result.output_ref is not None
        text = (tmp_path / Path(result.output_ref["label"])).read_text(encoding="utf-8")
        assert text.startswith("FROM_SUBAGENT:")
    finally:
        register_designer_subagent_runner(None)
