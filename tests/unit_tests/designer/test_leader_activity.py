# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from jiuwenswarm.common.schema.designer_graph import (
    ACTIVITY_KIND_STAGE,
    ACTIVITY_KIND_THINKING,
    ACTIVITY_KIND_TOOL_CALL,
    LEADER_NODE_ID,
    NODE_STATUS_RUNNING,
    apply_node_activity,
    is_leader_node_id,
    normalize_node_state,
)
from jiuwenswarm.server.runtime.designer.activity import (
    apply_run_activity,
    graph_node_states,
    should_publish,
)
from jiuwenswarm.server.runtime.designer.leader_chat import (
    apply_leader_plan,
    message_asks_to_run,
)


def test_normalize_node_state_keeps_activity() -> None:
    state = normalize_node_state(
        {
            "status": NODE_STATUS_RUNNING,
            "activity": {"kind": ACTIVITY_KIND_TOOL_CALL, "text": "calling designer_graph_patch", "tool": "designer_graph_patch", "at": 1},
            "activity_tail": ["calling designer_graph_patch", "building"],
        }
    )
    assert state["activity"]["tool"] == "designer_graph_patch"
    assert len(state["activity_tail"]) == 2


def test_apply_node_activity_caps_tail() -> None:
    state = {"status": NODE_STATUS_RUNNING}
    for index in range(12):
        state = apply_node_activity(state, kind=ACTIVITY_KIND_STAGE, text=f"step {index}")
    assert len(state["activity_tail"]) == 8
    assert state["activity_tail"][-1].endswith("11")
    assert [item["text"] for item in state["activity_log"]] == [
        f"step {index}" for index in range(12)
    ]


def test_apply_node_activity_keeps_structured_agent_progress() -> None:
    state = apply_node_activity(
        {"status": NODE_STATUS_RUNNING},
        kind=ACTIVITY_KIND_THINKING,
        text="planning the keyframe",
        at=1,
    )
    state = apply_node_activity(
        state,
        kind=ACTIVITY_KIND_TOOL_CALL,
        text="calling call_image_model",
        tool="call_image_model",
        at=2,
    )

    normalized = normalize_node_state(state)
    assert normalized["activity_log"] == [
        {
            "kind": ACTIVITY_KIND_THINKING,
            "text": "planning the keyframe",
            "at": 1,
        },
        {
            "kind": ACTIVITY_KIND_TOOL_CALL,
            "text": "calling call_image_model",
            "tool": "call_image_model",
            "at": 2,
        },
    ]


def test_graph_node_states_skips_leader() -> None:
    run = {
        "node_states": {
            "n_brief": {"status": "completed"},
            LEADER_NODE_ID: {"status": NODE_STATUS_RUNNING},
        }
    }
    states = graph_node_states(run)
    assert "n_brief" in states
    assert LEADER_NODE_ID not in states
    assert is_leader_node_id(LEADER_NODE_ID)


def test_activity_publish_throttles() -> None:
    assert should_publish("run_a", "n_1", now=1000, force=True) is True
    assert should_publish("run_a", "n_1", now=1100) is False
    assert should_publish("run_a", "n_1", now=1400) is True


def test_apply_run_activity_creates_leader_state() -> None:
    run = {"run_id": "run_1", "node_states": {}}
    apply_run_activity(run, LEADER_NODE_ID, kind=ACTIVITY_KIND_STAGE, text="reading brief")
    assert run["node_states"][LEADER_NODE_ID]["status"] == NODE_STATUS_RUNNING
    assert run["node_states"][LEADER_NODE_ID]["activity"]["text"]


def _sample_graph() -> dict:
    return {
        "schema_version": "designer-execution-graph.v1",
        "graph_id": "graph_test",
        "project_id": "p1",
        "title": "Demo",
        "nodes": [
            {
                "id": "n_brief",
                "type": "text",
                "label": "Text 1",
                "config": {"role": "text", "pipeline": "brief", "prompt": "alley at night"},
                "layout": {"x": 40, "y": 240, "width": 280, "height": 160},
            },
            {
                "id": "n_character",
                "type": "image",
                "label": "Image 1",
                "config": {"role": "image", "pipeline": "character_design", "prompt": "officer"},
                "layout": {"x": 400, "y": 40, "width": 280, "height": 160},
            },
            {
                "id": "n_compose",
                "type": "video",
                "label": "Video 2",
                "config": {"role": "video", "pipeline": "compose"},
                "layout": {"x": 1200, "y": 240, "width": 280, "height": 160},
            },
        ],
        "edges": [
            {
                "id": "e_brief_character",
                "source": "n_brief",
                "target": "n_character",
                "kind": "data",
            }
        ],
        "created_at": 1,
        "updated_at": 1,
    }


def test_apply_leader_plan_refine_selected_node() -> None:
    graph = _sample_graph()
    plan = {
        "intent": "refine_node",
        "summary": "Updated Character",
        "patch": {
            "upsert_nodes": [
                {
                    "id": "n_character",
                    "type": "image",
                    "label": "Character",
                    "config": {"role": "character_design", "prompt": "把角色改得更赛博"},
                }
            ]
        },
        "run_node_ids": ["n_character"],
    }
    next_graph, run_ids, summary = apply_leader_plan(graph, plan)
    assert run_ids == ["n_character"]
    char = next(node for node in next_graph["nodes"] if node["id"] == "n_character")
    assert "赛博" in str(char["config"]["prompt"])
    assert "Updated" in summary or "Character" in summary


def test_apply_leader_plan_stamps_type_for_inserted_scene_and_clip() -> None:
    from jiuwenswarm.common.schema.designer_graph import DesignerGraphValidationError

    graph = _sample_graph()
    plan = {
        "intent": "edit_graph",
        "summary": "插入咖啡豆特写",
        "patch": {
            "upsert_nodes": [
                {
                    "id": "n_scene_4",
                    "config": {"prompt": "咖啡豆特写，无人物", "setting_id": "set_4"},
                },
                {
                    "id": "n_clip_4",
                    "config": {
                        "shot_index": 2,
                        "timeline": "5-8s",
                        "shot_action": "咖啡豆静置，画面中无人物",
                        "prompt": "咖啡豆特写",
                    },
                },
            ],
            "upsert_edges": [
                {"id": "e_scene4_clip4", "source": "n_scene_4", "target": "n_clip_4"},
            ],
        },
    }
    next_graph, run_ids, _summary = apply_leader_plan(graph, plan)
    assert run_ids == []
    scene = next(node for node in next_graph["nodes"] if node["id"] == "n_scene_4")
    clip = next(node for node in next_graph["nodes"] if node["id"] == "n_clip_4")
    assert scene["type"] == "image"
    assert scene["config"]["pipeline"] == "scene"
    assert clip["type"] == "video"
    assert clip["config"]["pipeline"] == "clip"
    assert clip["config"]["timeline"] == "5-8s"
    assert any(
        edge.get("source") == "n_scene_4" and edge.get("target") == "n_clip_4"
        for edge in next_graph.get("edges") or []
    )

    try:
        apply_leader_plan(
            graph,
            {
                "intent": "edit_graph",
                "summary": "未知节点",
                "patch": {"upsert_nodes": [{"id": "n_extra", "config": {"prompt": "x"}}]},
            },
        )
    except DesignerGraphValidationError as exc:
        assert "n_extra" in str(exc)
    else:
        raise AssertionError("untyped unknown node should be rejected")


def test_apply_leader_plan_add_node_without_run() -> None:
    graph = _sample_graph()
    plan = {
        "intent": "edit_graph",
        "summary": "Added audio",
        "patch": {
            "upsert_nodes": [
                {
                    "id": "n_audio_1",
                    "type": "audio",
                    "label": "Audio",
                    "config": {"role": "audio", "prompt": "配乐"},
                }
            ],
            "upsert_edges": [
                {
                    "id": "e_audio_compose",
                    "source": "n_audio_1",
                    "target": "n_compose",
                    "kind": "data",
                }
            ],
        },
        "run_node_ids": [],
    }
    next_graph, run_ids, _summary = apply_leader_plan(graph, plan)
    assert run_ids == []
    types = {node["type"] for node in next_graph["nodes"]}
    assert "audio" in types
    assert any(
        edge.get("source") == "n_audio_1" and edge.get("target") == "n_compose"
        for edge in next_graph.get("edges") or []
    )


def test_message_asks_to_run_detects_generate_intent() -> None:
    assert message_asks_to_run("加一个配乐节点并生成") is True
    assert message_asks_to_run("加一个配乐节点接到成片，先别生成") is False


def test_tool_result_activity_text_keeps_progress_lines_readable() -> None:
    from jiuwenswarm.server.runtime.designer.node_agent import _tool_result_activity_text

    assert (
        _tool_result_activity_text("read_upstream", '[{"node_id": "n_character"}]')
        == "read_upstream done"
    )
    assert (
        _tool_result_activity_text("designer_graph_get", '{"graph": {"graph_id": "g1"}}')
        == "designer_graph_get done"
    )
    assert _tool_result_activity_text("call_image_model", "") == "call_image_model done"
    assert (
        _tool_result_activity_text("call_image_model", "[ERROR]: Image generation failed")
        == "[ERROR]: Image generation failed"
    )
    assert (
        _tool_result_activity_text("ffmpeg_compose", "composed 4 clips\ninto final.mp4")
        == "composed 4 clips into final.mp4"
    )
