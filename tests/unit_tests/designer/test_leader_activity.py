# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import pytest

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
    _EN_NEGATABLE_VERBS,
    _ZH_ACTION_VERBS,
    _node_prompt_for_snapshot,
    _snapshot_nodes,
    _stage_run_summary,
    _storyboard_edit_requested,
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


@pytest.mark.parametrize(
    "message",
    [
        "加一个配乐节点，不要生成",
        "加一个配乐节点，不要重新生成",
        "加一个配乐节点，不要运行",
        "加一个配乐节点，不要重跑",
        "加一个配乐节点，不用生成",
        "加一个配乐节点，不用运行",
        "加一个配乐节点，不用跑",
        "加一个配乐节点，无需生成",
        "加一个配乐节点，无需运行",
        "加一个配乐节点，暂不生成",
        "加一个配乐节点，暂不运行",
        "加一个配乐节点，不需要生成",
        "加一个配乐节点，不需要运行",
        "加一个配乐节点，先不生成",
        "加一个配乐节点，先不运行",
        "别生成配乐",
        "别运行这个节点",
        "without generating the score",
        "without running the compose node",
        "don't generate the score",
        "don't run the compose node",
        "do not generate the score",
        "do not run the compose node",
    ],
)
def test_message_asks_to_run_ignores_negative_instructions(message: str) -> None:
    assert message_asks_to_run(message) is False


@pytest.mark.parametrize(
    "message",
    [
        "不要开始",
        "不要继续",
        "不要确认",
        "不要合成",
        "不要拼接",
        "不要出片",
        "不要重新合成",
        "别开始",
        "不用继续",
        "无需确认",
        "暂不继续",
        "不需要开始",
        "先不开始",
        "don't proceed",
        "don't compose",
        "don't stitch",
        "don't rerun",
        "do not proceed",
        "do not confirm",
        "without composing the film",
        "無需生成",
        "暫不生成",
        "別開始",
        "別運行",
        "先別開始",
        "不要繼續",
    ],
)
def test_message_asks_to_run_ignores_negated_verbs(message: str) -> None:
    assert message_asks_to_run(message) is False


# The leader tells the user to reply 「确认」, but a traditional-Chinese user sends
# 確認. Simplified-only matching read that as "no run requested", so the guard in
# run_leader_chat wiped run_node_ids and the confirmation produced no asset while
# the reply claimed generation had started.
@pytest.mark.parametrize(
    "message",
    [
        "確認",
        "確定",
        "開始",
        "繼續",
        "沒問題",
        "就這樣",
        "下個步驟",
        "運行",
        "確認，開始生成",
    ],
)
def test_traditional_acknowledgement_asks_to_run(message: str) -> None:
    assert message_asks_to_run(message) is True


# The negation list is built from the same constants _RUN_HINT uses, so these two
# tests are the guard that keeps the two patterns in step: add a verb to
# _ZH_ACTION_VERBS / _EN_NEGATABLE_VERBS and it is instantly required to have a
# working negated form.
@pytest.mark.parametrize(
    "negation",
    ["不要", "别", "別", "不用", "无需", "無需", "暂不", "暫不", "不需要", "先不"],
)
@pytest.mark.parametrize("verb", _ZH_ACTION_VERBS.split("|"))
def test_every_chinese_run_verb_is_negatable(negation: str, verb: str) -> None:
    assert message_asks_to_run(negation + verb) is False


@pytest.mark.parametrize("verb", _EN_NEGATABLE_VERBS.split("|"))
def test_every_english_run_verb_is_negatable(verb: str) -> None:
    assert message_asks_to_run("don't " + verb) is False
    assert message_asks_to_run("do not " + verb) is False


@pytest.mark.parametrize(
    "message",
    [
        "不需要修改，生成吧",
        "别的不说，直接生成",
        "无需改动，运行吧",
        "不用改，直接运行吧",
        "不要改，开始吧",
        "别的不说，开始生成",
        "不用管我，继续",
        "无需多言，直接出片",
        "generate the score without the voiceover",
        "compose it, don't wait for me",
        "run it, don't wait for me",
    ],
)
def test_message_asks_to_run_keeps_positive_instructions(message: str) -> None:
    assert message_asks_to_run(message) is True


def test_snapshot_reports_the_prompt_a_clip_node_actually_uses() -> None:
    node = {
        "id": "n_clip_2",
        "type": "video",
        "label": "Shot 2",
        "config": {"pipeline": "clip", "generate": {"prompt": "flying through the canopy"}},
    }
    assert _node_prompt_for_snapshot(node) == "flying through the canopy"


def test_snapshot_prompt_falls_back_to_shot_action() -> None:
    node = {
        "id": "n_clip_3",
        "type": "video",
        "label": "Shot 3",
        "config": {"pipeline": "clip", "shot_action": "lands on the branch"},
    }
    assert _node_prompt_for_snapshot(node) == "lands on the branch"


def test_snapshot_reports_whether_a_node_already_has_an_output() -> None:
    graph = {
        "graph_id": "graph_1",
        "nodes": [
            {
                "id": "n_clip_1",
                "type": "video",
                "label": "Shot 1",
                "config": {"pipeline": "clip"},
                "output_ref": {"uri": "file:///tmp/shot1.mp4", "kind": "video"},
            },
            {
                "id": "n_clip_2",
                "type": "video",
                "label": "Shot 2",
                "config": {"pipeline": "clip"},
            },
        ],
        "edges": [],
    }
    by_id = {node["id"]: node for node in _snapshot_nodes(graph)}
    assert by_id["n_clip_1"]["has_output"] is True
    assert by_id["n_clip_2"]["has_output"] is False


def test_snapshot_treats_a_placeholder_output_as_not_built() -> None:
    graph = {
        "graph_id": "graph_1",
        "nodes": [
            {
                "id": "n_x",
                "config": {"pipeline": "clip"},
                "output_ref": {"uri": "designer://pending"},
            }
        ],
        "edges": [],
    }
    assert _snapshot_nodes(graph)[0]["has_output"] is False


@pytest.mark.parametrize(
    "message",
    [
        "调整分镜。至少 4 秒",
        "調整分鏡。每個至少 4 秒",
        "调整分镜。每個至少 4 秒。兩個分鏡就好",
        "修改 storyboard 的时长",
        "重新生成分镜脚本",
    ],
)
def test_storyboard_edit_request_is_detected(message: str) -> None:
    assert _storyboard_edit_requested(message) is True


@pytest.mark.parametrize(
    "message",
    [
        "给我看看分镜脚本",
        "分镜没问题的话回复「确认」",
        "show me the storyboard",
        "what is the status now?",
    ],
)
def test_read_only_storyboard_question_is_not_an_edit(message: str) -> None:
    assert _storyboard_edit_requested(message) is False


def test_stage_run_summary_names_the_stage_that_will_actually_run() -> None:
    graph = {
        "graph_id": "graph_1",
        "nodes": [
            {
                "id": "n_storyboard",
                "type": "table",
                "config": {"pipeline": "storyboard", "prompt": "x"},
            },
            {"id": "n_clip_1", "type": "video", "config": {"pipeline": "clip"}},
        ],
        "edges": [],
    }
    # The reply promised the character sheet; the run built the storyboard.
    assert "分镜脚本" in _stage_run_summary(["n_storyboard"], graph, chinese=True)
    assert "shot videos" in _stage_run_summary(["n_clip_1"], graph, chinese=False)


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
