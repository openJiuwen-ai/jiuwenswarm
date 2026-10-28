# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for Designer graph store and normalize."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from jiuwenswarm.common.schema.designer_graph import (
    NODE_ROLE_CLIP,
    NODE_ROLE_COMPOSE,
    NODE_TYPE_TEXT,
    NODE_TYPE_VIDEO,
    NODE_STATUS_COMPLETED,
    RUN_STATUS_COMPLETED,
    DesignerGraphValidationError,
    expand_shot_nodes,
    preserve_expanded_shot_nodes,
    normalize_execution_graph,
    node_pipeline,
    repair_overlapping_pipeline_layout,
)
from jiuwenswarm.server.runtime.designer.graph_store import DesignerGraphStore
from tests.unit_tests.design.graph_fixtures import make_pipeline_graph


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
def designer_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_director_llm: None
) -> DesignerGraphStore:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.graph_store.get_agent_root_dir",
        lambda: tmp_path,
    )
    return DesignerGraphStore()


def test_graph_store_roundtrip(designer_store: DesignerGraphStore) -> None:
    graph = make_pipeline_graph(project_id="proj_test01", prompt="roundtrip")
    saved = designer_store.save_graph(graph)
    loaded = designer_store.get_graph(saved["graph_id"])
    assert loaded is not None
    assert loaded["graph_id"] == saved["graph_id"]
    assert loaded["project_id"] == "proj_test01"


def test_graph_and_run_ids_cannot_escape_their_directories(
    designer_store: DesignerGraphStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Path separators in graph_id / run_id must not read, write, or delete outside the store."""
    graph = make_pipeline_graph(project_id="proj_path01", prompt="path confinement")
    saved = designer_store.save_graph(graph)
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(saved), encoding="utf-8")
    absolute_id = str(outside.with_suffix(""))

    assert designer_store.get_graph(absolute_id) is None
    assert designer_store.delete_graph(absolute_id) is False
    assert outside.is_file()

    for bad_id in (absolute_id, "../escaped", "..", ".", "C:\\Windows\\win.ini", "proj:1", "a/b", "ok\x00id"):
        payload = deepcopy(graph)
        payload["graph_id"] = bad_id
        with pytest.raises(DesignerGraphValidationError, match="path separators"):
            designer_store.save_graph(payload)
        assert designer_store.get_graph(bad_id) is None
        assert designer_store.delete_graph(bad_id) is False
        run = {
            "schema_version": "designer-execution-run.v1",
            "run_id": bad_id,
            "graph_id": saved["graph_id"],
            "project_id": "proj_path01",
            "status": "draft",
            "node_states": {},
            "current_node_ids": [],
        }
        with pytest.raises(DesignerGraphValidationError, match="path separators"):
            designer_store.save_run(run)
        assert designer_store.get_run(bad_id) is None

    assert not (tmp_path / "designer" / "escaped.json").exists()
    assert designer_store.get_graph(saved["graph_id"]) is not None

    localized = deepcopy(graph)
    localized["graph_id"] = "镜头.甲-1"
    stored = designer_store.save_graph(localized)
    assert stored["graph_id"] == "镜头.甲-1"
    assert designer_store.get_graph("镜头.甲-1")["project_id"] == "proj_path01"
    assert (tmp_path / "designer" / "graphs" / "镜头.甲-1.json").is_file()


def test_expand_shot_nodes_creates_one_clip_per_shot() -> None:
    graph = make_pipeline_graph(project_id="proj_expand01", prompt="three shots")
    expanded = expand_shot_nodes(graph, 3)
    clip_ids = [node["id"] for node in expanded["nodes"] if node_pipeline(node) == NODE_ROLE_CLIP]
    assert clip_ids == ["n_clip_1", "n_clip_2", "n_clip_3"]
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


def test_preserve_node_output_refs_keeps_storyboard_uri() -> None:
    from jiuwenswarm.common.schema.designer_graph import preserve_node_output_refs

    existing = make_pipeline_graph(project_id="proj_keep_ref", prompt="keep ref")
    story = next(node for node in existing["nodes"] if node["id"] == "n_storyboard")
    story["output_ref"] = {
        "kind": "table",
        "uri": "file:///tmp/storyboard.md",
        "mime_type": "text/markdown",
        "label": "storyboard.md",
    }
    incoming = make_pipeline_graph(project_id="proj_keep_ref", prompt="keep ref")
    incoming["graph_id"] = existing["graph_id"]
    merged = preserve_node_output_refs(incoming, existing)
    kept = next(node for node in merged["nodes"] if node["id"] == "n_storyboard")
    assert kept["output_ref"]["uri"] == "file:///tmp/storyboard.md"


def test_expand_preserves_existing_generate_prompt() -> None:
    graph = make_pipeline_graph(project_id="proj_keep_prompt", prompt="keep prompt")
    clip = next(node for node in graph["nodes"] if node["id"] == "n_clip_1")
    clip["config"]["generate"] = {
        "prompt": "用户改过的画面",
        "prompt_origin": "user",
    }
    expanded = expand_shot_nodes(graph, 2)
    kept = next(node for node in expanded["nodes"] if node["id"] == "n_clip_1")
    assert kept["config"]["generate"]["prompt"] == "用户改过的画面"
    assert kept["config"]["generate"]["prompt_origin"] == "user"


def test_repair_overlapping_compose_and_column_layout() -> None:
    graph = make_pipeline_graph(project_id="proj_layout01", prompt="old overlap")
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
    restored = repair_overlapping_pipeline_layout(graph)
    compose = next(node for node in restored["nodes"] if node["id"] == "n_compose")
    clip = next(node for node in restored["nodes"] if node["id"] == "n_clip_1")
    clip_layout = clip.get("layout") or {}
    compose_layout = compose.get("layout") or {}
    assert compose_layout["x"] >= float(clip_layout.get("x") or 0) + float(clip_layout.get("width") or 280)
    _assert_nodes_do_not_overlap(restored["nodes"])


def test_preserve_expanded_shot_nodes_rejects_stale_bootstrap_save() -> None:
    graph = make_pipeline_graph(project_id="proj_keep01", prompt="keep shots")
    expanded = expand_shot_nodes(graph, 3)
    stale = make_pipeline_graph(project_id="proj_keep01", prompt="keep shots")
    stale["graph_id"] = expanded["graph_id"]
    kept = preserve_expanded_shot_nodes(stale, expanded)
    assert [node["id"] for node in kept["nodes"] if node_pipeline(node) == NODE_ROLE_CLIP] == [
        "n_clip_1",
        "n_clip_2",
        "n_clip_3",
    ]


def test_graph_store_does_not_shrink_expanded_shot_nodes(designer_store: DesignerGraphStore) -> None:
    graph = designer_store.save_graph(
        expand_shot_nodes(
            make_pipeline_graph(project_id="proj_keep02", prompt="keep shots"),
            3,
        )
    )
    stale = make_pipeline_graph(project_id="proj_keep02", prompt="keep shots")
    stale["graph_id"] = graph["graph_id"]
    saved = designer_store.save_graph(stale)
    assert [node["id"] for node in saved["nodes"] if node_pipeline(node) == NODE_ROLE_CLIP] == [
        "n_clip_1",
        "n_clip_2",
        "n_clip_3",
    ]


def test_preserve_expanded_shot_nodes_keeps_user_deleted_extra_shots() -> None:
    graph = make_pipeline_graph(project_id="proj_del01", prompt="delete extra shots")
    expanded = expand_shot_nodes(graph, 4)
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
        expand_shot_nodes(
            make_pipeline_graph(project_id="proj_del02", prompt="delete extra shots"),
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
    graph = make_pipeline_graph(project_id="proj_del03", prompt="delete extra shots")
    expanded = expand_shot_nodes(graph, 4)
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


def test_graph_store_list_by_project(designer_store: DesignerGraphStore) -> None:
    first = designer_store.save_graph(
        make_pipeline_graph(project_id="proj_list_a", prompt="first"),
    )
    second = designer_store.save_graph(
        make_pipeline_graph(project_id="proj_list_a", prompt="second"),
    )
    designer_store.save_graph(
        make_pipeline_graph(project_id="proj_list_b", prompt="other project"),
    )
    graphs = designer_store.list_graphs_for_project("proj_list_a")
    graph_ids = {graph["graph_id"] for graph in graphs}
    assert graph_ids == {first["graph_id"], second["graph_id"]}
    all_ids = {graph["graph_id"] for graph in designer_store.list_graphs()}
    assert first["graph_id"] in all_ids
    assert second["graph_id"] in all_ids


def test_normalize_keeps_unique_clip_ids() -> None:
    """Frame→frame chain stripping was removed with the frame pipeline; normalize still
    accepts a valid multi-clip graph with unique ids."""
    graph = expand_shot_nodes(
        make_pipeline_graph(project_id="proj_chain01", prompt="unique clips"),
        2,
    )
    restored = normalize_execution_graph(graph)
    clip_ids = [
        node["id"] for node in restored["nodes"] if node_pipeline(node) == NODE_ROLE_CLIP
    ]
    assert clip_ids == ["n_clip_1", "n_clip_2"]
    assert len(clip_ids) == len(set(clip_ids))


def test_director_chain_media_settings_follow_the_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    from jiuwenswarm.server.runtime.design.director_chain import _media_settings_for
    from jiuwenswarm.server.runtime.designer.media_intent import MediaIntent

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.pipeline.model_capacity.configured_video_model_id",
        lambda: "wan3.0-video",
    )
    graph = {"graph_id": "g_media", "project_id": "default", "metadata": {}}

    video, image = _media_settings_for(graph, MediaIntent(aspect_ratio="9:16", film_duration_sec=20))
    assert video.as_dict() == {"aspect_ratio": "9:16", "resolution": "1080P"}
    assert image.as_dict() == {"aspect_ratio": "9:16", "resolution": "1K"}

    graph["metadata"] = {"video_settings": {"aspect_ratio": "1:1", "resolution": "720P"}}
    video, _image = _media_settings_for(graph, MediaIntent())
    assert video.as_dict() == {"aspect_ratio": "1:1", "resolution": "720P"}


def test_start_run_rejects_run_from_another_graph(
    designer_store: DesignerGraphStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiuwenswarm.server.runtime.gateway_adapter import designer_adapter as adapter

    first = designer_store.save_graph(
        make_pipeline_graph(project_id="proj_iso_a", prompt="scheme a"),
    )
    second = designer_store.save_graph(
        make_pipeline_graph(project_id="proj_iso_b", prompt="scheme b"),
    )
    run = designer_store.save_run(
        {
            "schema_version": "designer-execution-run.v1",
            "run_id": "run_iso_a",
            "graph_id": first["graph_id"],
            "project_id": "proj_iso_a",
            "status": RUN_STATUS_COMPLETED,
            "node_states": {},
            "current_node_ids": [],
        }
    )
    monkeypatch.setattr(adapter, "_store", designer_store)
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
        make_pipeline_graph(project_id="proj_sum01", prompt="clip ready"),
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
        make_pipeline_graph(project_id="proj_slim01", prompt="slim listing"),
    )

    payload, error, code = adapter._list_graphs({})

    assert error is None
    assert code is None
    assert payload is not None
    listed = next(item for item in payload["graphs"] if item["graph_id"] == graph["graph_id"])
    assert set(listed) <= {
        "graph_id",
        "project_id",
        "project_dir",
        "project_name",
        "title",
        "updated_at",
        "schema_version",
    }
    assert "nodes" not in listed
    assert "edges" not in listed
    assert graph["graph_id"] in {item["graph_id"] for item in payload["summaries"]}


def test_get_graph_hydrates_node_outputs_from_latest_run(
    designer_store: DesignerGraphStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiuwenswarm.server.runtime.gateway_adapter import designer_adapter as adapter

    monkeypatch.setattr(adapter, "_store", designer_store)
    graph = designer_store.save_graph(
        make_pipeline_graph(project_id="proj_hydrate01", prompt="train arrives"),
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
