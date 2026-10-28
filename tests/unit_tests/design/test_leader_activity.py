# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from jiuwenswarm.common.schema.designer_graph import (
    ACTIVITY_KIND_STAGE,
    ACTIVITY_KIND_THINKING,
    ACTIVITY_KIND_TOOL_CALL,
    LEADER_NODE_ID,
    NODE_STATUS_RUNNING,
    apply_node_activity,
    normalize_node_state,
)
from jiuwenswarm.server.runtime.designer.activity import (
    apply_run_activity,
    should_publish,
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
        text="planning the clip",
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
            "text": "planning the clip",
            "at": 1,
        },
        {
            "kind": ACTIVITY_KIND_TOOL_CALL,
            "text": "calling call_image_model",
            "tool": "call_image_model",
            "at": 2,
        },
    ]


def test_activity_publish_throttles() -> None:
    assert should_publish("run_a", "n_1", now=1000, force=True) is True
    assert should_publish("run_a", "n_1", now=1100) is False
    assert should_publish("run_a", "n_1", now=1400) is True


def test_apply_run_activity_creates_leader_state() -> None:
    run = {"run_id": "run_1", "node_states": {}}
    apply_run_activity(run, LEADER_NODE_ID, kind=ACTIVITY_KIND_STAGE, text="reading brief")
    assert run["node_states"][LEADER_NODE_ID]["status"] == NODE_STATUS_RUNNING
    assert run["node_states"][LEADER_NODE_ID]["activity"]["text"]


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
