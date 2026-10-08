# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import pytest

from jiuwenswarm.common.schema.designer_graph import (
    NODE_STATUS_PENDING,
    break_cycles_for_schedule,
    execution_predecessors,
    filter_ready_by_dependency_order,
    raw_execution_predecessors,
)
from jiuwenswarm.server.runtime.designer.executor import _is_ready


def _graph(nodes, edges):
    return {
        "schema_version": "designer-execution-graph.v1",
        "graph_id": "g1",
        "project_id": "p1",
        "title": "t",
        "description": "",
        "nodes": nodes,
        "edges": edges,
        "metadata": {},
    }


def test_config_inputs_count_as_schedule_deps_even_without_edge():
    graph = _graph(
        [
            {"id": "a", "type": "image", "config": {"role": "frame", "shot_index": 1}},
            {
                "id": "b",
                "type": "image",
                "config": {
                    "role": "frame",
                    "shot_index": 2,
                    "inputs": ["a"],
                    "scene_prompt_handoff_from": "a",
                },
            },
        ],
        [],  # no edges — soft config dep only
    )
    preds = execution_predecessors(graph)
    assert "a" in preds["b"]
    run = {
        "node_states": {
            "a": {"status": NODE_STATUS_PENDING},
            "b": {"status": NODE_STATUS_PENDING},
        }
    }
    assert _is_ready("a", run, preds)
    assert not _is_ready("b", run, preds)


def test_cycle_breaks_by_shot_priority_so_dependent_waits():
    graph = _graph(
        [
            {"id": "n_clip_1", "type": "video", "config": {"role": "clip", "shot_index": 1}},
            {"id": "n_clip_2", "type": "video", "config": {"role": "clip", "shot_index": 2}},
        ],
        [
            {"id": "e12", "source": "n_clip_1", "target": "n_clip_2", "kind": "data"},
            {"id": "e21", "source": "n_clip_2", "target": "n_clip_1", "kind": "data"},  # cycle
        ],
    )
    raw = raw_execution_predecessors(graph)
    assert "n_clip_1" in raw["n_clip_2"] and "n_clip_2" in raw["n_clip_1"]
    dag = break_cycles_for_schedule(raw, graph)
    # Keep forward 1→2; drop back-edge 2→1
    assert "n_clip_1" in dag["n_clip_2"]
    assert "n_clip_2" not in dag["n_clip_1"]
    run = {
        "node_states": {
            "n_clip_1": {"status": NODE_STATUS_PENDING},
            "n_clip_2": {"status": NODE_STATUS_PENDING},
        }
    }
    assert _is_ready("n_clip_1", run, dag)
    assert not _is_ready("n_clip_2", run, dag)


def test_same_level_ready_set_never_starts_dependent_with_peer_pred():
    graph = _graph(
        [
            {"id": "n_clip_1", "type": "video", "config": {"role": "clip", "shot_index": 1}},
            {"id": "n_clip_2", "type": "video", "config": {"role": "clip", "shot_index": 2}},
            {"id": "n_char_1", "type": "image", "config": {"role": "character_design"}},
        ],
        [
            {"id": "e12", "source": "n_clip_1", "target": "n_clip_2", "kind": "data"},
        ],
    )
    preds = execution_predecessors(graph)
    # Simulate a buggy ready set that includes both ends of an edge.
    selected = filter_ready_by_dependency_order(
        ["n_clip_1", "n_clip_2", "n_char_1"],
        preds=preds,
        in_flight=set(),
        graph=graph,
    )
    assert "n_clip_1" in selected
    assert "n_char_1" in selected
    assert "n_clip_2" not in selected


def test_soft_clip_dep_unlocks_when_prior_prompt_artifact_ready():
    from jiuwenswarm.common.schema.designer_graph import (
        NODE_STATUS_PENDING,
        NODE_STATUS_RUNNING,
        artifact_dependency_satisfied,
        is_soft_artifact_dependency,
    )
    from jiuwenswarm.server.runtime.designer.executor import _is_ready

    graph = _graph(
        [
            {
                "id": "n_clip_1",
                "type": "video",
                "config": {
                    "role": "clip",
                    "shot_index": 1,
                    "handoff_artifact_ready": True,
                    "last_wan_prompt": "shot1 wan prompt already used for tools",
                    "continuity_card": {
                        "already_done": ["shot 1: onset already happened — do not restart the run"],
                        "prior_action_summary": "man starts running",
                    },
                },
            },
            {
                "id": "n_clip_2",
                "type": "video",
                "config": {
                    "role": "clip",
                    "shot_index": 2,
                    "continuity_clip_node_id": "n_clip_1",
                    "previous_clip_wan_prompt": "shot1 wan prompt already used for tools",
                    "previous_clip_handoff_ready": True,
                },
            },
        ],
        [{"id": "e12", "source": "n_clip_1", "target": "n_clip_2", "kind": "data"}],
    )
    assert is_soft_artifact_dependency(graph, "n_clip_1", "n_clip_2")
    assert artifact_dependency_satisfied(graph, "n_clip_2", "n_clip_1")
    preds = execution_predecessors(graph)
    run = {
        "node_states": {
            "n_clip_1": {"status": NODE_STATUS_RUNNING},
            "n_clip_2": {"status": NODE_STATUS_PENDING},
        }
    }
    assert _is_ready("n_clip_2", run, preds, graph)
    selected = filter_ready_by_dependency_order(
        ["n_clip_2"],
        preds=preds,
        in_flight={"n_clip_1"},
        graph=graph,
    )
    assert selected == ["n_clip_2"]


def test_later_clip_waits_until_prior_wan_prompt_exists():
    graph = _graph(
        [
            {
                "id": "n_clip_1",
                "type": "video",
                "config": {"role": "clip", "shot_index": 1, "shot_action": "father sits down"},
            },
            {
                "id": "n_clip_2",
                "type": "video",
                "config": {"role": "clip", "shot_index": 2, "shot_action": "child answers"},
            },
        ],
        [{"id": "e12", "source": "n_clip_1", "target": "n_clip_2", "kind": "data"}],
    )
    preds = execution_predecessors(graph)
    run = {
        "node_states": {
            "n_clip_1": {"status": "running"},
            "n_clip_2": {"status": NODE_STATUS_PENDING},
        }
    }
    assert not _is_ready("n_clip_2", run, preds, graph)

    graph["nodes"][0]["config"]["handoff_artifact_ready"] = True
    graph["nodes"][0]["config"]["last_wan_prompt"] = (
        "Image 1 is Father.\nImage 2 is the dining room.\nFather sits down."
    )
    assert _is_ready("n_clip_2", run, preds, graph)
    selected = filter_ready_by_dependency_order(
        ["n_clip_1", "n_clip_2"],
        preds=preds,
        in_flight=set(),
        graph=graph,
        run=run,
    )
    assert selected == ["n_clip_1", "n_clip_2"]


def test_clip_starts_while_scene_is_still_running_once_plate_exists():
    graph = _graph(
        [
            {"id": "n_scene", "type": "image", "config": {"role": "scene"}},
            {
                "id": "n_clip_1",
                "type": "video",
                "config": {"role": "clip", "shot_index": 1, "inputs": ["n_scene"]},
            },
        ],
        [{"id": "e", "source": "n_scene", "target": "n_clip_1", "kind": "data"}],
    )
    preds = execution_predecessors(graph)
    run = {
        "node_states": {
            "n_scene": {
                "status": "running",
                "output_ref": {
                    "kind": "image",
                    "uri": "file:///tmp/plate.png",
                    "mime_type": "image/png",
                },
            },
            "n_clip_1": {"status": NODE_STATUS_PENDING},
        }
    }
    assert _is_ready("n_clip_1", run, preds, graph)
    selected = filter_ready_by_dependency_order(
        ["n_clip_1"],
        preds=preds,
        in_flight={"n_scene"},
        graph=graph,
        run=run,
    )
    assert selected == ["n_clip_1"]


def test_compose_stays_locked_while_a_clip_is_still_running():
    graph = _graph(
        [
            {"id": "n_clip_1", "type": "video", "config": {"role": "clip", "shot_index": 1, "shot_action": "sit"}},
            {
                "id": "n_compose",
                "type": "video",
                "config": {"role": "compose", "inputs": ["n_clip_1"]},
            },
        ],
        [{"id": "e", "source": "n_clip_1", "target": "n_compose", "kind": "data"}],
    )
    preds = execution_predecessors(graph)
    run = {
        "node_states": {
            "n_clip_1": {
                "status": "running",
                "output_ref": {
                    "kind": "video",
                    "uri": "file:///tmp/clip.mp4",
                    "mime_type": "video/mp4",
                },
            },
            "n_compose": {"status": NODE_STATUS_PENDING},
        }
    }
    assert not _is_ready("n_compose", run, preds, graph)
    selected = filter_ready_by_dependency_order(
        ["n_compose"],
        preds=preds,
        in_flight={"n_clip_1"},
        graph=graph,
        run=run,
    )
    assert selected == []


def test_compose_waits_for_all_clips_not_soft_unlocked(tmp_path):
    from jiuwenswarm.common.schema.designer_graph import (
        NODE_STATUS_COMPLETED,
        NODE_STATUS_PENDING,
        NODE_STATUS_RUNNING,
        compose_required_predecessor_ids,
        is_soft_artifact_dependency,
    )
    from jiuwenswarm.server.runtime.designer.executor import _is_ready

    def _vid(name: str) -> dict:
        path = tmp_path / name
        path.write_bytes(b"m" * 600)
        return {
            "kind": "video",
            "uri": path.resolve().as_uri(),
            "mime_type": "video/mp4",
        }

    graph = _graph(
        [
            {"id": "n_clip_1", "type": "video", "config": {"role": "clip", "shot_index": 1}},
            {"id": "n_clip_2", "type": "video", "config": {"role": "clip", "shot_index": 2}},
            {"id": "n_clip_3", "type": "video", "config": {"role": "clip", "shot_index": 3}},
            {
                "id": "n_compose",
                "type": "video",
                "config": {
                    "role": "compose",
                    # Intentionally incomplete inputs — scheduler must still wait for all clips.
                    "inputs": ["n_clip_3"],
                },
            },
        ],
        [
            {"id": "e3c", "source": "n_clip_3", "target": "n_compose", "kind": "data"},
        ],
    )
    required = compose_required_predecessor_ids(graph)
    assert set(required) == {"n_clip_1", "n_clip_2", "n_clip_3"}
    assert not is_soft_artifact_dependency(graph, "n_clip_1", "n_compose")
    assert not is_soft_artifact_dependency(graph, "n_clip_3", "n_compose")

    preds = execution_predecessors(graph)
    run = {
        "node_states": {
            "n_clip_1": {"status": NODE_STATUS_COMPLETED, "output_ref": _vid("c1.mp4")},
            "n_clip_2": {"status": NODE_STATUS_RUNNING},
            "n_clip_3": {"status": NODE_STATUS_COMPLETED, "output_ref": _vid("c3.mp4")},
            "n_compose": {"status": NODE_STATUS_PENDING},
        }
    }
    assert not _is_ready("n_compose", run, preds, graph)

    run["node_states"]["n_clip_2"] = {
        "status": NODE_STATUS_COMPLETED,
        "output_ref": _vid("c2.mp4"),
    }
    assert _is_ready("n_compose", run, preds, graph)


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
                    "config": {"role": NODE_ROLE_CLIP, "shot_index": 1},
                },
                {
                    "id": "n_clip_2",
                    "type": NODE_TYPE_VIDEO,
                    "config": {"role": NODE_ROLE_CLIP, "shot_index": 2},
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


def test_compose_also_waits_for_connected_audio_nodes(tmp_path):
    from jiuwenswarm.common.schema.designer_graph import (
        NODE_STATUS_COMPLETED,
        NODE_STATUS_PENDING,
        compose_required_predecessor_ids,
    )
    from jiuwenswarm.server.runtime.designer.executor import _is_ready

    clip = tmp_path / "c1.mp4"
    clip.write_bytes(b"m" * 600)
    speech = tmp_path / "s.wav"
    speech.write_bytes(b"a" * 128)
    music = tmp_path / "m.wav"
    music.write_bytes(b"a" * 128)
    clip_ref = {"kind": "video", "uri": clip.resolve().as_uri(), "mime_type": "video/mp4"}
    speech_ref = {"kind": "audio", "uri": speech.resolve().as_uri(), "mime_type": "audio/wav"}
    music_ref = {"kind": "audio", "uri": music.resolve().as_uri(), "mime_type": "audio/wav"}

    graph = _graph(
        [
            {"id": "n_clip_1", "type": "video", "config": {"role": "clip", "shot_index": 1}},
            {"id": "n_speech", "type": "audio", "config": {"role": "speech"}},
            {"id": "n_music", "type": "audio", "config": {"role": "music"}},
            {"id": "n_music_unused", "type": "audio", "config": {"role": "music"}},
            {
                "id": "n_compose",
                "type": "video",
                "config": {"role": "compose", "inputs": ["n_clip_1", "n_speech"]},
            },
        ],
        [
            {"id": "e", "source": "n_clip_1", "target": "n_compose", "kind": "data"},
            {"id": "e_music", "source": "n_music", "target": "n_compose", "kind": "data"},
        ],
    )
    required = compose_required_predecessor_ids(graph)
    assert required == ["n_clip_1", "n_music", "n_speech"]
    preds = execution_predecessors(graph)
    run = {
        "node_states": {
            "n_clip_1": {"status": NODE_STATUS_COMPLETED, "output_ref": clip_ref},
            "n_speech": {"status": NODE_STATUS_PENDING},
            "n_music": {"status": NODE_STATUS_COMPLETED, "output_ref": music_ref},
            "n_compose": {"status": NODE_STATUS_PENDING},
        }
    }
    assert not _is_ready("n_compose", run, preds, graph)
    run["node_states"]["n_speech"] = {"status": NODE_STATUS_COMPLETED, "output_ref": speech_ref}
    assert _is_ready("n_compose", run, preds, graph)

    # COMPLETED without a real audio file must not unlock compose.
    run["node_states"]["n_speech"] = {
        "status": NODE_STATUS_COMPLETED,
        "output_ref": {"kind": "audio", "uri": (tmp_path / "missing.wav").as_uri()},
    }
    assert not _is_ready("n_compose", run, preds, graph)


def test_hard_keyframe_dep_still_blocks_until_complete():
    from jiuwenswarm.common.schema.designer_graph import NODE_STATUS_PENDING, NODE_STATUS_RUNNING
    from jiuwenswarm.server.runtime.designer.executor import _is_ready

    graph = _graph(
        [
            {"id": "n_frame_1", "type": "image", "config": {"role": "frame", "shot_index": 1}},
            {
                "id": "n_clip_1",
                "type": "video",
                "config": {"role": "clip", "shot_index": 1, "inputs": ["n_frame_1"]},
            },
        ],
        [{"id": "e", "source": "n_frame_1", "target": "n_clip_1", "kind": "data"}],
    )
    preds = execution_predecessors(graph)
    run = {
        "node_states": {
            "n_frame_1": {"status": NODE_STATUS_RUNNING},
            "n_clip_1": {"status": NODE_STATUS_PENDING},
        }
    }
    assert not _is_ready("n_clip_1", run, preds, graph)
