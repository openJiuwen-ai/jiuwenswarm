# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""deepresearch_stream router 纯函数单测。"""
import json

import pytest

from jiuwenswarm.agents.harness.common.tools.deepresearch import (
    stream_router as stream_router_module,
)
from jiuwenswarm.agents.harness.common.tools.deepresearch.stream_router import (
    MAX_ACCUMULATED_TEXT_CHARS,
    MAX_CHUNK_TEXT_CHARS,
    RouterState,
    advance_stage,
    build_interrupt_prompt,
    collected_questions,
    complete_final_report_processing,
    route_chunk,
    start_final_report_processing,
    _format_outline_card_markdown,
)


STAGE_TITLES = [
    "研究主题澄清",
    "大纲生成",
    "并行调研与章节撰写",
    "报告交付",
]


def _stage_update(frames):
    updates = [
        frame for frame in frames if frame["event_type"] == "task.update"
    ]
    return updates[-1] if updates else None


def _process_reasoning(frames):
    return [
        frame for frame in frames
        if frame["event_type"] == "chat.reasoning" and frame.get("stream_source_id")
    ]


def _assert_stage(update, active_stage):
    assert update is not None
    assert [task["task_id"] for task in update["tasks"]] == [
        f"deepresearch_stage_{index}" for index in range(1, 5)
    ]
    assert [task["task_content"] for task in update["tasks"]] == STAGE_TITLES
    expected = [
        "completed" if index < active_stage
        else "in_progress" if index == active_stage
        else "pending"
        for index in range(1, 5)
    ]
    assert [task["status"] for task in update["tasks"]] == expected
    assert update["total_tasks"] == 4
    assert update["completed_tasks"] == active_stage - 1
    assert update["in_progress_tasks"] == 1
    assert update["pending_tasks"] == 4 - active_stage


def _deep_json(depth):
    value = "leaf"
    for _ in range(depth):
        value = {"nested": value}
    return value


class _CountingList(list):
    """Track historical element visits without relying on wall-clock timing."""

    def __init__(self, values):
        super().__init__(values)
        self.items_visited = 0

    def __iter__(self):
        for item in super().__iter__():
            self.items_visited += 1
            yield item

    def reset_visits(self):
        self.items_visited = 0


def test_advance_stage_emits_status_only_to_task_list():
    frames = advance_stage(RouterState(), 1)

    assert [frame["event_type"] for frame in frames] == ["task.update"]
    _assert_stage(frames[0], 1)


def test_advance_stage_backfills_every_missing_stage_in_event_order():
    state = RouterState(current_stage=2)

    frames = advance_stage(state, 4)

    assert [frame["event_type"] for frame in frames] == [
        "task.update",
        "task.update",
    ]
    updates = [
        frame for frame in frames if frame["event_type"] == "task.update"
    ]
    assert [
        next(
            index
            for index, task in enumerate(update["tasks"], start=1)
            if task["status"] == "in_progress"
        )
        for update in updates
    ] == [3, 4]
    assert state.current_stage == 4


def test_advance_stage_marks_previous_stage_complete_in_next_snapshot():
    state = RouterState(current_stage=1)

    frames = advance_stage(state, 2)

    assert [frame["event_type"] for frame in frames] == ["task.update"]
    _assert_stage(_stage_update(frames), 2)


def test_stage_2_uses_outline_generation_title_in_task_list():
    frames = advance_stage(RouterState(), 2)

    assert [frame["event_type"] for frame in frames] == [
        "task.update",
        "task.update",
    ]
    assert frames[-1]["tasks"][1]["task_content"] == "大纲生成"


def test_advance_stage_completion_keeps_all_four_completed_tasks_visible():
    state = RouterState()
    advance_stage(state, 4)

    frames = advance_stage(state, 4, complete=True)

    assert [frame["event_type"] for frame in frames] == ["task.update"]
    update = frames[0]
    assert len(update["tasks"]) == 4
    assert [task["task_id"] for task in update["tasks"]] == [
        f"deepresearch_stage_{index}" for index in range(1, 5)
    ]
    assert all(task["status"] == "completed" for task in update["tasks"])


def test_advance_stage_does_not_repeat_or_regress_snapshots():
    state = RouterState()

    assert advance_stage(state, 4)
    assert advance_stage(state, 4) == []
    assert advance_stage(state, 2) == []


def test_completed_snapshot_is_monotonic():
    state = RouterState()

    advance_stage(state, 4, complete=True)

    assert advance_stage(state, 2) == []
    assert state.current_stage == 4
    assert state.stages_completed is True


def test_workflow_nodes_advance_four_stage_snapshot():
    state = RouterState()

    for agent, stage in (
        ("intent_recognition", 1),
        ("outline", 2),
        ("editor_team", 3),
    ):
        _assert_stage(_stage_update(route_chunk({"agent": agent}, state)), stage)

    assert _stage_update(route_chunk({"agent": "reporter"}, state)) is None
    assert _stage_update(route_chunk({"agent": "source_tracer"}, state)) is None


def test_interrupt_nodes_advance_stage_before_raw_chunk_is_suppressed():
    feedback = route_chunk(
        {"agent": "feedback_handler", "message_type": "interrupt"},
        RouterState(),
    )
    outline = route_chunk(
        {"agent": "outline_interaction", "message_type": "interrupt"},
        RouterState(),
    )

    _assert_stage(_stage_update(feedback), 1)
    _assert_stage(_stage_update(outline), 2)


def test_stage_snapshot_never_regresses_on_late_earlier_node():
    state = RouterState()
    advance_stage(state, 4)

    frames = route_chunk({"agent": "outline", "content": "迟到的大纲事件"}, state)

    assert _stage_update(frames) is None


def test_stage_one_node_uses_explicit_stage_parent_without_task_boundaries():
    state = RouterState()
    chunk = {
        "agent": "intent_recognition",
        "event": "start",
        "reasoning_content": "分析研究需求",
    }
    frames = route_chunk(chunk, state)

    assert not any(frame["event_type"] in {"task.start", "task.complete"} for frame in frames)
    reasoning = _process_reasoning(frames)
    assert [frame["content"] for frame in reasoning] == [
        "意图识别开始\n",
        "分析研究需求",
    ]
    assert all(frame["task_id"] == "deepresearch_stage_1" for frame in reasoning)
    assert state.active_nodes["intent_recognition"]["started"] is True


def test_outline_reasoning_is_nested_under_stage_two():
    frames = route_chunk(
        {"agent": "outline", "event": "start", "content": "大纲正文"},
        RouterState(),
    )

    assert _process_reasoning(frames) == [{
        "event_type": "chat.reasoning",
        "task_id": "deepresearch_stage_2",
        "task_content": "大纲生成 - 规划报告章节结构",
        "stream_source_id": "dr_outline",
        "content": "大纲正文",
    }]
    assert not any(
        frame["event_type"] == "task.start" and frame.get("task_id") == "dr_outline"
        for frame in frames
    )


def test_same_node_second_chunk_no_duplicate_start():
    state = RouterState()
    route_chunk({"agent": "intent_recognition", "content": "a"}, state)
    frames = route_chunk({"agent": "intent_recognition", "content": "b"}, state)
    assert frames == []


def test_stage_one_event_done_emits_stage_scoped_reasoning():
    state = RouterState()
    route_chunk({"agent": "intent_recognition", "content": "a"}, state)
    frames = route_chunk({"agent": "intent_recognition", "event": "done"}, state)
    assert _process_reasoning(frames) == [{
        "event_type": "chat.reasoning",
        "task_id": "deepresearch_stage_1",
        "task_content": "意图识别 - 分析研究需求",
        "stream_source_id": "dr_intent_recognition",
        "content": "意图识别完成\n",
    }]
    assert not any(frame["event_type"] == "task.complete" for frame in frames)


def test_parallel_section_nodes_emit_one_section_boundary():
    expected = {
        "plan_reasoning": "规划调研",
        "collector_query_generation": "生成检索词",
        "collector_info_retrieval": "资料检索",
        "collector_supervisor": "采集评估",
        "collector_summary": "资料汇总",
        "sub_reporter": "章节撰写",
    }

    for agent, display_name in expected.items():
        state = RouterState()
        started = route_chunk(
            {
                "agent": agent,
                "section_idx": "3",
                "section_title": "真实章节标题",
                "event": "start",
                "content": "",
            },
            state,
        )
        completed = route_chunk(
            {"agent": agent, "section_idx": "3", "event": "done", "content": ""},
            state,
        )
        validated = (
            route_chunk(
                {
                    "agent": agent,
                    "section_idx": "3",
                    "event": "summary_response",
                    "content": "SUCCESS",
                },
                state,
            )
            if agent == "sub_reporter"
            else []
        )

        started_reasoning = _process_reasoning(started)
        assert started_reasoning == [{
            "event_type": "chat.reasoning",
            "task_id": "deepresearch_stage_3",
            "task_content": "真实章节标题",
            "task_index": 3,
            "stream_source_id": "deepresearch_section_3",
            "content": f"{display_name}开始\n",
        }]
        completed_reasoning = _process_reasoning(completed)
        assert completed_reasoning == [{
            "event_type": "chat.reasoning",
            "task_id": "deepresearch_stage_3",
            "task_content": "真实章节标题",
            "task_index": 3,
            "stream_source_id": "deepresearch_section_3",
            "content": f"{display_name}完成\n",
        }]
        _assert_stage(_stage_update(started), 3)
        starts = [frame for frame in started if frame["event_type"] == "task.start"]
        assert starts == [{
            "event_type": "task.start",
            "task_id": "deepresearch_stage_3",
            "task_content": "真实章节标题",
            "task_index": 3,
            "stream_source_id": "deepresearch_section_3",
        }]
        completes = [
            frame
            for frame in completed + validated
            if frame["event_type"] == "task.complete"
        ]
        assert len(completes) == (1 if agent == "sub_reporter" else 0)


def test_parallel_sections_use_explicit_stage_three_parent_with_own_boundary():
    state = RouterState()

    section_frames = route_chunk(
        {
            "agent": "plan_reasoning",
            "section_idx": "1",
            "section_title": "真实章节标题",
            "event": "start",
        },
        state,
    )
    section_boundaries = [
        frame for frame in section_frames
        if frame["event_type"] in {"task.start", "task.complete"}
    ]
    assert [frame["event_type"] for frame in section_boundaries] == ["task.start"]
    assert section_boundaries[0]["task_id"] == "deepresearch_stage_3"
    assert section_boundaries[0]["stream_source_id"] == "deepresearch_section_1"
    section_reasoning = _process_reasoning(section_frames)
    assert all(frame["task_id"] == "deepresearch_stage_3" for frame in section_reasoning)
    assert all(frame["stream_source_id"] == "deepresearch_section_1" for frame in section_reasoning)


def test_final_report_nodes_share_stage_four_aggregate_stream():
    state = RouterState(section_titles={"1": "第一章"})
    route_chunk(
        {
            "agent": "sub_reporter",
            "section_idx": "1",
            "section_total": 1,
            "event": "summary_response",
            "content": "SUCCESS",
        },
        state,
    )
    frames = route_chunk(
        {"agent": "reporter", "event": "start", "reasoning_content": "整合报告"},
        state,
    )

    reasoning = _process_reasoning(frames)
    assert [frame["content"] for frame in reasoning] == ["报告整合开始\n", "整合报告"]
    assert all(frame["task_id"] == "deepresearch_stage_4" for frame in reasoning)
    assert all(frame["task_content"] == "最终报告处理" for frame in reasoning)
    assert all(frame["stream_source_id"] == "deepresearch_final_report" for frame in reasoning)
    assert not any(frame["event_type"].startswith("task.") and frame["event_type"] != "task.update" for frame in frames)


def test_final_report_aggregate_starts_only_after_all_sections_complete():
    state = RouterState(section_titles={"1": "第一章", "2": "第二章"})
    first = route_chunk(
        {
            "agent": "sub_reporter",
            "section_idx": "1",
            "section_total": 2,
            "event": "summary_response",
            "content": "SUCCESS",
        },
        state,
    )
    second = route_chunk(
        {
            "agent": "sub_reporter",
            "section_idx": "2",
            "section_total": 2,
            "event": "summary_response",
            "content": "SUCCESS",
        },
        state,
    )

    assert not any(
        frame.get("stream_source_id") == "deepresearch_final_report"
        for frame in first
    )
    assert [
        (frame["event_type"], frame["stream_source_id"])
        for frame in second
        if frame["event_type"] in {"task.start", "task.complete"}
    ] == [
        ("task.start", "deepresearch_section_2"),
        ("task.complete", "deepresearch_section_2"),
        ("task.start", "deepresearch_final_report"),
    ]
    stage_four_update_index = next(
        index for index, frame in enumerate(second)
        if frame["event_type"] == "task.update"
        and frame["tasks"][3]["status"] == "in_progress"
    )
    aggregate_start_index = next(
        index for index, frame in enumerate(second)
        if frame["event_type"] == "task.start"
        and frame.get("stream_source_id") == "deepresearch_final_report"
    )
    assert stage_four_update_index < aggregate_start_index
    assert second[aggregate_start_index]["task_id"] == "deepresearch_stage_4"


def test_final_report_waits_for_every_expected_section():
    state = RouterState(section_titles={"1": "第一章", "2": "第二章"})

    first = route_chunk(
        {
            "agent": "sub_reporter",
            "section_idx": "1",
            "section_total": 2,
            "event": "summary_response",
            "content": "SUCCESS",
        },
        state,
    )
    assert not any(
        frame.get("stream_source_id") == "deepresearch_final_report"
        for frame in first
    )

    frames = route_chunk(
        {
            "agent": "sub_reporter",
            "section_idx": "2",
            "section_total": 2,
            "event": "summary_response",
            "content": "SUCCESS",
        },
        state,
    )
    assert any(
        frame.get("event_type") == "task.start"
        and frame.get("stream_source_id") == "deepresearch_final_report"
        for frame in frames
    )


def test_final_report_cannot_shrink_authoritative_section_set():
    state = RouterState(section_titles={"1": "第一章", "2": "第二章"})

    frames = route_chunk(
        {
            "agent": "sub_reporter",
            "section_idx": "1",
            "section_total": 1,
            "event": "done",
        },
        state,
    )

    assert state.expected_section_total == 2
    assert not any(
        frame.get("stream_source_id") == "deepresearch_final_report"
        for frame in frames
    )


def test_section_total_has_small_deterministic_bound():
    state = RouterState(section_titles={"1": "第一章"})

    with pytest.raises(
        ValueError, match="^deepresearch_router_limit_exceeded$"
    ):
        route_chunk(
            {
                "agent": "sub_reporter",
                "section_idx": "1",
                "section_total": 10**18,
            "event": "summary_response",
            "content": "SUCCESS",
            },
            state,
        )

    assert state.completed_section_indices == set()
    assert state.final_report_started is False


@pytest.mark.parametrize("content", [_deep_json(1500)])
def test_deep_json_fails_with_fixed_router_error(content):
    with pytest.raises(ValueError, match="^deepresearch_router_limit_exceeded$"):
        route_chunk(
            {
                "agent": "plan_reasoning",
                "section_idx": "1",
                "section_total": 1,
                "event": "message",
                "content": content,
            },
            RouterState(),
        )


def test_cyclic_json_fails_with_fixed_router_error():
    content = {}
    content["cycle"] = content

    with pytest.raises(ValueError, match="^deepresearch_router_limit_exceeded$"):
        route_chunk(
            {
                "agent": "plan_reasoning",
                "section_idx": "1",
                "section_total": 1,
                "event": "message",
                "content": content,
            },
            RouterState(),
        )


@pytest.mark.parametrize("field", ["agent", "section_idx", "message_id"])
def test_oversized_identifier_is_rejected_before_state_mutation(field):
    state = RouterState()
    chunk = {
        "agent": "question_generator",
        "message_type": "message_chunk",
        "message_id": "message-1",
        "content": "question",
        field: "x" * 1025,
    }

    with pytest.raises(ValueError, match="^deepresearch_router_limit_exceeded$"):
        route_chunk(chunk, state)

    assert state.question_parts == {}
    assert state.active_nodes == {}


def test_oversized_single_chunk_is_rejected_before_state_mutation():
    state = RouterState()

    with pytest.raises(ValueError, match="^deepresearch_router_limit_exceeded$"):
        route_chunk(
            {
                "agent": "reporter",
                "event": "message",
                "content": "secret" * 200_000,
            },
            state,
        )

    assert state.report_parts == []
    assert state.pending_final_report_frames == []


@pytest.mark.parametrize(
    ("state", "chunk"),
    [
        (
            RouterState(report_parts=["x" * 1_048_576]),
            {"agent": "reporter", "content": "x"},
        ),
        (
            RouterState(outline_parts=["x" * 1_048_576]),
            {"agent": "outline", "content": "x"},
        ),
        (
            RouterState(
                question_parts={"m": ["x" * 1_048_576]},
                question_order=["m"],
            ),
            {
                "agent": "question_generator",
                "message_type": "message_chunk",
                "message_id": "m",
                "content": "x",
            },
        ),
        (
            RouterState(
                pending_final_report_frames=[
                    {"event_type": "chat.reasoning", "content": "x" * 1_048_576}
                ]
            ),
            {"agent": "reporter", "event": "start", "content": "x"},
        ),
        (
            RouterState(
                active_nodes={
                    f"node-{index}": {"started": True, "done": False}
                    for index in range(256)
                }
            ),
            {"agent": "intent_recognition", "content": "x"},
        ),
        (
            RouterState(
                section_titles={str(index): "title" for index in range(1, 257)}
            ),
            {
                "agent": "plan_reasoning",
                "section_idx": "257",
                "section_total": 257,
                "content": "x",
            },
        ),
    ],
)
def test_cumulative_router_state_is_bounded(state, chunk):
    with pytest.raises(ValueError, match="^deepresearch_router_limit_exceeded$"):
        route_chunk(chunk, state)


def test_router_does_not_rescan_accumulated_text_history_per_chunk():
    report_parts = _CountingList(["existing report"])
    outline_parts = _CountingList(["existing outline"])
    question_fragments = _CountingList(["existing question"])
    pending_frames = _CountingList([
        {"event_type": "chat.reasoning", "content": "existing pending"}
    ])
    state = RouterState(
        report_parts=report_parts,
        outline_parts=outline_parts,
        question_parts={"message-1": question_fragments},
        question_order=["message-1"],
        pending_final_report_frames=pending_frames,
    )
    for values in (
        report_parts,
        outline_parts,
        question_fragments,
        pending_frames,
    ):
        values.reset_visits()

    for index in range(8):
        route_chunk(
            {"agent": "reporter", "content": f"new report {index}"},
            state,
        )

    assert [
        report_parts.items_visited,
        outline_parts.items_visited,
        question_fragments.items_visited,
        pending_frames.items_visited,
    ] == [0, 0, 0, 0]


def test_router_state_initializes_text_counters_once_and_rejects_oversize():
    report_parts = _CountingList(["report"])
    pending_frames = _CountingList([
        {"event_type": "chat.reasoning", "content": "pending"}
    ])

    state = RouterState(
        report_parts=report_parts,
        pending_final_report_frames=pending_frames,
    )

    assert report_parts.items_visited == 1
    assert pending_frames.items_visited == 1
    assert state._accumulated_text_chars == len("reportpending")
    assert state._pending_final_report_text_chars == len("pending")
    with pytest.raises(ValueError, match="^deepresearch_router_limit_exceeded$"):
        RouterState(report_parts=["x" * (MAX_ACCUMULATED_TEXT_CHARS + 1)])


def test_pending_final_report_flush_releases_only_pending_text_count():
    state = RouterState(
        report_parts=["retained"],
        pending_final_report_frames=[
            {"event_type": "chat.reasoning", "content": "pending-1"},
            {"event_type": "chat.reasoning", "content": "pending-2"},
        ],
    )
    before = state._accumulated_text_chars

    frames = start_final_report_processing(state)

    assert [
        frame["content"]
        for frame in frames
        if frame.get("event_type") == "chat.reasoning"
        and frame.get("content", "").startswith("pending-")
    ] == ["pending-1", "pending-2"]
    assert state.pending_final_report_frames == []
    assert state._pending_final_report_text_chars == 0
    assert state._accumulated_text_chars == (
        before - len("pending-1pending-2")
    ) == len("retained")


def test_failed_text_append_leaves_router_state_and_counter_unchanged():
    state = RouterState(report_parts=["x" * MAX_ACCUMULATED_TEXT_CHARS])
    before_parts = list(state.report_parts)
    before_count = state._accumulated_text_chars

    with pytest.raises(ValueError, match="^deepresearch_router_limit_exceeded$"):
        route_chunk({"agent": "reporter", "content": "y"}, state)

    assert state.report_parts == before_parts
    assert state._accumulated_text_chars == before_count
    assert state.pending_final_report_frames == []
    assert state.active_nodes == {}


def test_failed_pending_append_rolls_back_report_and_node_state():
    retained = "x" * (MAX_ACCUMULATED_TEXT_CHARS - 1)
    state = RouterState(report_parts=[retained])
    before_count = state._accumulated_text_chars

    with pytest.raises(ValueError, match="^deepresearch_router_limit_exceeded$"):
        route_chunk({"agent": "reporter", "content": "y"}, state)

    assert state.report_parts == [retained]
    assert state.pending_final_report_frames == []
    assert state.active_nodes == {}
    assert state._accumulated_text_chars == before_count
    assert state._pending_final_report_text_chars == 0


def test_final_report_boundaries_remain_exactly_once():
    state = RouterState(section_titles={"1": "第一章"})
    frames = []
    frames.extend(route_chunk(
        {
            "agent": "sub_reporter",
            "section_idx": "1",
            "section_total": 1,
            "event": "summary_response",
            "content": "SUCCESS",
        },
        state,
    ))
    frames.extend(route_chunk(
        {
            "agent": "sub_reporter",
            "section_idx": "1",
            "section_total": 1,
                "event": "summary_response",
                "content": "SUCCESS",
        },
        state,
    ))
    frames.extend(complete_final_report_processing(state))
    frames.extend(complete_final_report_processing(state))

    boundaries = [
        frame["event_type"]
        for frame in frames
        if frame.get("stream_source_id") == "deepresearch_final_report"
        and frame["event_type"] in {"task.start", "task.complete"}
    ]
    assert boundaries == ["task.start", "task.complete"]


def test_final_report_reasoning_waits_for_all_sections_then_follows_start_boundary():
    state = RouterState(section_titles={"1": "第一章", "2": "第二章"})
    route_chunk(
        {
            "agent": "sub_reporter",
            "section_idx": "1",
            "section_total": 2,
            "event": "summary_response",
            "content": "SUCCESS",
        },
        state,
    )

    early = route_chunk(
        {"agent": "reporter", "event": "start", "reasoning_content": "提前到达的整合内容"},
        state,
    )
    assert not any(
        frame.get("stream_source_id") == "deepresearch_final_report"
        for frame in early
    )

    released = route_chunk(
        {
            "agent": "sub_reporter",
            "section_idx": "2",
            "section_total": 2,
            "event": "summary_response",
            "content": "SUCCESS",
        },
        state,
    )
    aggregate = [
        frame
        for frame in released
        if frame.get("stream_source_id") == "deepresearch_final_report"
    ]
    assert aggregate[0]["event_type"] == "task.start"
    assert [frame["content"] for frame in aggregate[1:]] == [
        "报告整合开始\n",
        "提前到达的整合内容",
    ]


def test_final_report_aggregate_completes_after_stage_four_starts():
    state = RouterState(section_titles={"1": "第一章"})
    frames = route_chunk(
        {
            "agent": "sub_reporter",
            "section_idx": "1",
            "section_total": 1,
            "event": "summary_response",
            "content": "SUCCESS",
        },
        state,
    )

    frames.extend(complete_final_report_processing(state))

    stage_four = next(
        index for index, frame in enumerate(frames)
        if frame["event_type"] == "task.update"
        and frame["tasks"][3]["status"] == "in_progress"
    )
    aggregate_start = next(
        index for index, frame in enumerate(frames)
        if frame["event_type"] == "task.start"
        and frame.get("stream_source_id") == "deepresearch_final_report"
    )
    aggregate_complete = next(
        index for index, frame in enumerate(frames)
        if frame["event_type"] == "task.complete"
        and frame.get("stream_source_id") == "deepresearch_final_report"
    )
    assert stage_four < aggregate_start < aggregate_complete
    assert frames[aggregate_complete]["task_id"] == "deepresearch_stage_4"


def test_last_section_reasoning_finishes_before_stage_four_transition():
    state = RouterState()
    base = {
        "agent": "sub_reporter",
        "section_idx": "1",
        "section_title": "第一章",
        "section_total": 1,
    }
    route_chunk({**base, "event": "start"}, state)

    frames = route_chunk(
        {
            **base,
            "event": "done",
            "reasoning_content": "正在完成章节最终检查",
        },
        state,
    )
    frames.extend(
        route_chunk(
            {**base, "event": "summary_response", "content": "SUCCESS"},
            state,
        )
    )

    final_section_reasoning = next(
        index for index, frame in enumerate(frames)
        if frame.get("content") == "正在完成章节最终检查"
    )
    stage_four_update = next(
        index for index, frame in enumerate(frames)
        if frame["event_type"] == "task.update"
        and frame["tasks"][3]["status"] == "in_progress"
    )
    assert final_section_reasoning < stage_four_update


def test_final_report_aggregate_cannot_complete_before_it_starts():
    state = RouterState(section_titles={"1": "第一章"})

    assert complete_final_report_processing(state) == []
    assert state.final_report_started is False
    assert state.final_report_completed is False


def test_parallel_section_reasoning_preserves_known_title_when_later_chunk_omits_it():
    state = RouterState()
    route_chunk(
        {
            "agent": "plan_reasoning",
            "section_idx": "1",
            "section_title": "真实标题",
            "section_total": 1,
            "event": "start",
        },
        state,
    )
    frames = route_chunk(
        {"agent": "collector_info_retrieval", "section_idx": "1", "event": "start"},
        state,
    )

    assert frames == [{
        "event_type": "chat.reasoning",
        "task_id": "deepresearch_stage_3",
        "task_content": "真实标题",
        "task_index": 1,
        "stream_source_id": "deepresearch_section_1",
        "content": "资料检索开始\n",
    }]


def test_parallel_section_reasoning_keeps_authoritative_outline_title():
    state = RouterState(section_titles={"2": "核心架构设计与检索增强能力深度对比"})
    frames = route_chunk(
        {
            "agent": "plan_reasoning",
            "section_idx": "2",
            "section_total": 3,
            "event": "message",
            "content": json.dumps({
                "title": "检索策略与索引能力信息采集",
                "thought": "完整规划过程",
            }, ensure_ascii=False),
        },
        state,
    )

    reasoning = _process_reasoning(frames)
    _assert_stage(_stage_update(frames), 3)
    assert all(frame["task_content"] == "核心架构设计与检索增强能力深度对比" for frame in reasoning)
    assert all(frame["stream_source_id"] == "deepresearch_section_2" for frame in reasoning)
    assert state.section_titles["2"] == "核心架构设计与检索增强能力深度对比"


def test_plan_reasoning_message_preserves_complete_content_in_readable_markdown():
    state = RouterState()
    frames = route_chunk(
        {
            "agent": "plan_reasoning",
            "section_idx": "1",
            "section_title": "第一章",
            "section_total": 1,
            "event": "message",
            "content": {
                "title": "梳理主流记忆框架的分类、架构与数据流",
                "thought": "这段详细推理必须完整进入思考过程。",
            },
        },
        state,
    )

    reasoning = _process_reasoning(frames)
    assert reasoning == [
        {
            "event_type": "chat.reasoning",
            "task_id": "deepresearch_stage_3",
            "task_content": "第一章",
            "task_index": 1,
            "total_tasks": 1,
            "stream_source_id": "deepresearch_section_1",
            "content": "规划调研开始\n",
        },
        {
            "event_type": "chat.reasoning",
            "task_id": "deepresearch_stage_3",
            "task_content": "第一章",
            "task_index": 1,
            "total_tasks": 1,
            "stream_source_id": "deepresearch_section_1",
            "content": (
                "#### 调研计划：梳理主流记忆框架的分类、架构与数据流\n\n"
                "调研思路：这段详细推理必须完整进入思考过程。"
            ),
        },
    ]


def test_collector_summary_response_preserves_long_text_and_line_breaks():
    state = RouterState()
    original = (
        "本轮已确认 Mem0、MemOS 与 Zep 的核心架构差异。\n\n"
        "证据覆盖长期记忆、图记忆和部署方式，后续将补充基准测试数据。"
        + "更多原始过程。" * 40
    )
    frames = route_chunk(
        {
            "agent": "collector_summary",
            "section_idx": "1",
            "section_title": "第一章",
            "section_total": 1,
            "event": "summary_response",
            "content": original,
        },
        state,
    )

    reasoning = _process_reasoning(frames)
    assert reasoning == [
        {
            "event_type": "chat.reasoning",
            "task_id": "deepresearch_stage_3",
            "task_content": "第一章",
            "task_index": 1,
            "total_tasks": 1,
            "stream_source_id": "deepresearch_section_1",
            "content": "资料汇总开始\n",
        },
        {
            "event_type": "chat.reasoning",
            "task_id": "deepresearch_stage_3",
            "task_content": "第一章",
            "task_index": 1,
            "total_tasks": 1,
            "stream_source_id": "deepresearch_section_1",
            "content": original,
        },
    ]
    assert "\n\n" in reasoning[1]["content"]
    assert len(reasoning[1]["content"]) > 120


def test_parallel_sections_keep_interleaved_process_in_explicit_section_tasks():
    state = RouterState()
    first = route_chunk({
        "agent": "collector_info_retrieval",
        "section_idx": "1",
        "section_title": "第一章",
        "event": "message",
        "content": "第一章原始检索过程",
    }, state)
    second = route_chunk({
        "agent": "collector_info_retrieval",
        "section_idx": "2",
        "section_title": "第二章",
        "event": "message",
        "content": "第二章原始检索过程",
    }, state)
    third = route_chunk({
        "agent": "collector_info_retrieval",
        "section_idx": "1",
        "event": "message",
        "content": "第一章后续过程",
    }, state)

    assert first[-1]["task_id"] == "deepresearch_stage_3"
    assert first[-1]["task_content"] == "第一章"
    assert second[-1]["task_id"] == "deepresearch_stage_3"
    assert second[-1]["task_content"] == "第二章"
    assert third == [{
        "event_type": "chat.reasoning",
        "task_id": "deepresearch_stage_3",
        "task_content": "第一章",
        "task_index": 1,
        "stream_source_id": "deepresearch_section_1",
        "content": "第一章后续过程",
    }]


def test_parallel_sections_emit_one_stage_update_without_chapter_snapshots():
    state = RouterState(section_titles={"1": "第一章", "2": "第二章"})

    started = route_chunk({
        "agent": "collector_info_retrieval",
        "section_idx": "1",
        "section_total": 2,
        "event": "start",
        "content": "第一章检索过程",
    }, state)
    completed = route_chunk({
        "agent": "sub_reporter",
        "section_idx": "1",
        "section_total": 2,
        "event": "done",
        "content": "SUCCESS",
    }, state)
    validated = route_chunk({
        "agent": "sub_reporter",
        "section_idx": "1",
        "section_total": 2,
        "event": "summary_response",
        "content": "SUCCESS",
    }, state)
    repeated_success = route_chunk({
        "agent": "sub_reporter",
        "section_idx": "1",
        "section_total": 2,
        "event": "summary_response",
        "content": "SUCCESS",
    }, state)

    _assert_stage(_stage_update(started), 3)
    assert _stage_update(completed) is None
    assert all(
        frame["stream_source_id"] == "deepresearch_section_1"
        for frame in _process_reasoning(started + completed)
    )
    assert [
        frame["event_type"]
        for frame in validated
        if frame.get("stream_source_id") == "deepresearch_section_1"
    ] == ["task.complete"]
    assert repeated_success == []


def test_parallel_section_emits_distinct_reasoning_and_content_without_compaction():
    state = RouterState()
    frames = route_chunk({
        "agent": "collector_supervisor",
        "section_idx": "2",
        "section_title": "第二章",
        "event": "message",
        "reasoning_content": "原始推理\n第二行",
        "content": ["证据 A", {"source": "原始来源"}],
    }, state)

    reasoning = _process_reasoning(frames)
    assert [frame["content"] for frame in reasoning] == [
        "采集评估开始\n",
        "原始推理\n第二行",
        json.dumps(["证据 A", {"source": "原始来源"}], ensure_ascii=False),
    ]
    assert all(frame["task_id"] == "deepresearch_stage_3" for frame in reasoning)


def test_parallel_section_filters_control_process_values():
    for value in (" ALL END ", "SECTION END", "", "   "):
        state = RouterState()
        route_chunk({
            "agent": "sub_reporter",
            "section_idx": "1",
            "section_title": "第一章",
            "event": "start",
        }, state)
        frames = route_chunk({
            "agent": "sub_reporter",
            "section_idx": "1",
            "event": "summary_response",
            "content": value,
        }, state)
        assert frames == []


def test_sub_reporter_success_response_does_not_restart_reasoning():
    state = RouterState()
    base = {"section_idx": "1", "section_title": "第一章", "section_total": 1}
    route_chunk(
        {
            **base,
            "agent": "collector_summary",
            "event": "summary_response",
            "content": "已确认三个框架的关键架构差异。",
        },
        state,
    )
    route_chunk({**base, "agent": "sub_reporter", "event": "start"}, state)
    route_chunk({**base, "agent": "sub_reporter", "event": "done"}, state)
    frames = route_chunk(
        {
            **base,
            "agent": "sub_reporter",
            "event": "summary_response",
            "content": "SUCCESS",
        },
        state,
    )

    assert _process_reasoning(frames) == []
    assert any(frame["event_type"] == "task.complete" for frame in frames)
    assert state.final_report_started is True


def test_sub_reporter_section_completes_only_after_validated_success_marker():
    state = RouterState()
    base = {
        "agent": "sub_reporter",
        "section_idx": "1",
        "section_title": "第一章",
        "section_total": 1,
    }

    done_frames = route_chunk({**base, "event": "done"}, state)

    assert state.completed_section_indices == set()
    assert not any(frame["event_type"] == "task.complete" for frame in done_frames)
    assert state.final_report_started is False

    success_frames = route_chunk(
        {**base, "event": "summary_response", "content": "SUCCESS"},
        state,
    )

    assert state.completed_section_indices == {"1"}
    assert any(
        frame["event_type"] == "task.complete"
        and frame.get("stream_source_id") == "deepresearch_section_1"
        for frame in success_frames
    )
    assert state.final_report_started is True


def test_sub_reporter_forwards_reasoning_without_streaming_chapter_body():
    state = RouterState()
    frames = route_chunk(
        {
            "agent": "sub_reporter",
            "section_idx": "1",
            "section_title": "第一章",
            "section_total": 1,
            "event": "message",
            "reasoning_content": "正在组织章节结构",
            "content": "# 第一章\n\n这是完整章节正文。",
        },
        state,
    )

    reasoning = _process_reasoning(frames)
    assert [frame["content"] for frame in reasoning] == [
        "章节撰写开始\n",
        "正在组织章节结构",
    ]


def test_interrupt_chunk_not_forwarded():
    state = RouterState()
    frames = route_chunk({"agent": "outline_interaction", "message_type": "interrupt"}, state)
    _assert_stage(_stage_update(frames), 2)
    assert [frame["event_type"] for frame in frames] == [
        "task.update",
        "task.update",
    ]


def test_status_marker_skipped():
    state = RouterState()
    frames = route_chunk({"__deepsearch_status__": "started", "conversation_id": "X"}, state)
    assert frames == []


def test_skip_node_no_frame():
    state = RouterState()
    assert route_chunk({"agent": "start"}, state) == []
    assert route_chunk({"agent": "end"}, state) == []


def test_successful_end_result_bypasses_process_display_text_limit():
    state = RouterState()
    content = json.dumps({"response_content": "x" * MAX_CHUNK_TEXT_CHARS})

    frames = route_chunk(
        {
            "agent": "end",
            "event": "summary_response",
            "section_idx": "0",
            "content": content,
        },
        state,
    )

    assert len(content) > MAX_CHUNK_TEXT_CHARS
    assert state.final_report_started is True
    assert frames[-1] == {
        "event_type": "task.start",
        "task_id": "deepresearch_stage_4",
        "task_content": "最终报告处理",
        "stream_source_id": "deepresearch_final_report",
    }


def test_successful_end_result_keeps_json_shape_limit(monkeypatch):
    monkeypatch.setattr(stream_router_module, "MAX_JSON_NODES", 4)
    content = json.dumps({
        "response_content": "# Final",
        "metadata": [1, 2, 3],
    })

    with pytest.raises(ValueError, match="deepresearch_router_limit_exceeded"):
        route_chunk(
            {
                "agent": "end",
                "event": "summary_response",
                "section_idx": "0",
                "content": content,
            },
            RouterState(),
        )


def test_end_result_with_error_event_does_not_crash():
    """EndNode event=error (exception_info present) must not crash, but delivery is gated."""
    state = RouterState()
    content = json.dumps({
        "response_content": "# Final report",
        "exception_info": "[212000]Error when generate sub report",
    })

    route_chunk(
        {
            "agent": "end",
            "event": "error",
            "section_idx": "0",
            "content": content,
        },
        state,
    )

    assert state.final_report_started is False


def test_end_result_with_error_event_bypasses_process_display_text_limit():
    """The 12:18 crash: report > 1 MiB with exception_info must use the 16 MiB terminal bound, not crash."""
    state = RouterState()
    content = json.dumps({
        "response_content": "x" * (MAX_CHUNK_TEXT_CHARS + 100),
        "exception_info": "[212000]Error when generate sub report",
    })

    route_chunk(
        {
            "agent": "end",
            "event": "error",
            "section_idx": "0",
            "content": content,
        },
        state,
    )

    assert len(content) > MAX_CHUNK_TEXT_CHARS
    assert state.final_report_started is False


def test_end_result_without_response_content_falls_through():
    """EndNode with event=error but no response_content is not a terminal result."""
    state = RouterState()
    content = json.dumps({"exception_info": "fatal error"})

    route_chunk(
        {
            "agent": "end",
            "event": "error",
            "section_idx": "0",
            "content": content,
        },
        state,
    )

    assert state.final_report_started is False


def test_all_end_marker_is_not_terminal():
    """The trailing 'ALL END' marker has agent=end but content is not valid JSON."""
    state = RouterState()

    route_chunk(
        {
            "agent": "end",
            "event": "summary_response",
            "section_idx": "0",
            "content": "ALL END",
        },
        state,
    )

    assert state.final_report_started is False


def test_unknown_node_no_frame():
    state = RouterState()
    assert route_chunk({"agent": "mystery_node", "content": "x"}, state) == []


def test_reasoning_content_emits_chat_reasoning():
    state = RouterState()
    frames = route_chunk({"agent": "outline", "reasoning_content": "thinking..."}, state)
    assert any(f["event_type"] == "chat.reasoning" and f["content"] == "thinking..." for f in frames)


def test_outline_content_emits_chat_reasoning_for_readonly_thinking_display():
    state = RouterState()
    frames = route_chunk({
        "agent": "outline",
        "event": "message",
        "content": "# 研究大纲\n\n1. 背景\n2. 方案对比",
    }, state)

    reasoning = _process_reasoning(frames)
    assert reasoning[0] == {
        "event_type": "chat.reasoning",
        "task_id": "deepresearch_stage_2",
        "task_content": "大纲生成 - 规划报告章节结构",
        "stream_source_id": "dr_outline",
        "content": "# 研究大纲\n\n1. 背景\n2. 方案对比",
    }


def test_question_chunks_are_aggregated_by_message_id_in_first_seen_order():
    state = RouterState()
    route_chunk({
        "agent": "question_generator",
        "message_type": "message_chunk",
        "message_id": "q1",
        "content": "1. 关注哪些市场",
    }, state)
    route_chunk({
        "agent": "question_generator",
        "message_type": "message_chunk",
        "message_id": "q2",
        "content": "2. 使用什么时间范围",
    }, state)
    route_chunk({
        "agent": "question_generator",
        "message_type": "message_chunk",
        "message_id": "q1",
        "content": "？\n",
    }, state)

    assert collected_questions(state) == "1. 关注哪些市场？\n2. 使用什么时间范围"


def test_question_cache_ignores_unrelated_chunks():
    state = RouterState()
    route_chunk({
        "agent": "info_collector",
        "message_type": "message_chunk",
        "message_id": "noise",
        "content": "检索过程",
    }, state)
    route_chunk({
        "agent": "question_generator",
        "message_type": "summary_response",
        "message_id": "noise-2",
        "content": "不是问题碎片",
    }, state)

    assert collected_questions(state) == ""


def test_section_node_uses_keyed_state():
    state = RouterState()
    route_chunk({"agent": "sub_reporter", "section_idx": "1", "content": "a"}, state)
    route_chunk({"agent": "sub_reporter", "section_idx": "2", "content": "b"}, state)
    # 两个 section 独立,各发一次 task.start
    assert len([k for k in state.active_nodes if k.startswith("1:") or k.startswith("2:")]) == 2


def test_report_content_accumulated():
    # reporter 和 outline 都需要在 terminal marker 缺字段时提供交互兜底。
    state = RouterState()
    route_chunk({"agent": "reporter", "content": "第一章正文\n"}, state)
    route_chunk({"agent": "reporter", "content": "第二章正文\n"}, state)
    route_chunk({"agent": "generate_questions", "content": "问题A"}, state)
    route_chunk({"agent": "outline", "content": "大纲..."}, state)
    assert "".join(state.report_parts) == "第一章正文\n第二章正文\n"
    assert "".join(state.outline_parts) == "大纲..."
    assert not hasattr(state, "questions_parts")  # 累积器已移除


def test_interrupt_chunk_captures_node_and_prompt():
    state = RouterState()
    route_chunk(
        {
            "agent": "outline_interaction",
            "message_type": "interrupt",
            "content": "大纲已生成,请确认",
            "conversation_id": "C1",
        },
        state,
    )
    assert state.interrupt_node_id == "outline_interaction"
    assert state.interrupt_raw_prompt == "大纲已生成,请确认"
    assert state.interrupt_conversation_id == "C1"


def test_build_interrupt_prompt_outline_from_marker_content():
    # (1) §Stage3(b) 读 marker.content(JSON 解析为 OutlineContent);marker 有 content → 用它
    state = RouterState()
    marker = {"content": "第一章 来自marker\n第二章 来自marker", "agent": "outline_interaction"}
    prompt = build_interrupt_prompt("outline_interaction", state, marker, "query")
    assert "来自marker" in prompt


def test_build_interrupt_prompt_outline_marker_empty_uses_accumulated_outline():
    # 实际 SDK 的 interrupt chunk 只含审批提示；大纲正文来自此前 outline chunk。
    state = RouterState()
    route_chunk({"agent": "outline", "content": "第一章 累积大纲"}, state)
    route_chunk(
        {"agent": "outline_interaction", "message_type": "interrupt", "content": "请审批大纲"},
        state,
    )
    prompt = build_interrupt_prompt(state.interrupt_node_id, state, {}, "研究主题X")
    assert "第一章 累积大纲" in prompt
    assert "请审批大纲" in prompt


def test_build_interrupt_prompt_outline_status_placeholder_uses_accumulated_outline():
    state = RouterState()
    route_chunk({"agent": "outline", "content": "# 第一章\n累积大纲正文"}, state)

    prompt = build_interrupt_prompt(
        "outline_interaction",
        state,
        {"content": "Round 1: waiting for user feedback."},
        "研究主题X",
    )

    assert "累积大纲正文" in prompt
    assert "Round 1: waiting for user feedback." not in prompt


def test_build_interrupt_prompt_feedback_falls_back_to_query():
    state = RouterState()  # marker 无透传,interrupt chunk 也无内容
    route_chunk(
        {"agent": "feedback_handler", "message_type": "interrupt", "content": ""}, state
    )
    prompt = build_interrupt_prompt(state.interrupt_node_id, state, {}, "研究主题X")
    assert "研究主题X" in prompt


def test_build_interrupt_prompt_feedback_prefers_marker_prompt():
    state = RouterState()
    marker = {"prompt": "研究进行中,请输入反馈意见：", "agent": "feedback_handler"}
    prompt = build_interrupt_prompt("feedback_handler", state, marker, "研究主题X")
    assert "研究进行中" in prompt
    assert "研究主题X" not in prompt  # marker.prompt 优先,不退到 query fallback


def test_build_interrupt_prompt_ufp_truncates_report():
    # report 不在 marker,必须累积
    state = RouterState()
    route_chunk({"agent": "reporter", "content": "R" * 7000}, state)
    route_chunk(
        {
            "agent": "user_feedback_processor",
            "message_type": "interrupt",
            "content": "请选择后续操作",
        },
        state,
    )
    prompt = build_interrupt_prompt(state.interrupt_node_id, state, {}, "q")
    assert len(prompt) < 6200  # 截断 6000 + 占位
    assert "完整报告见最终产物" in prompt


def test_outline_json_is_rendered_as_readable_markdown():
    state = RouterState()
    frames = route_chunk(
        {
            "agent": "outline",
            "event": "message",
            "content": json.dumps(
                {
                    "id": "outline-1",
                    "language": "zh-CN",
                    "thought": "先分析市场，再比较竞争格局。",
                    "title": "行业分析",
                    "sections": [
                        {
                            "id": "section-1",
                            "title": "市场现状",
                            "description": "分析市场规模与增长驱动因素。",
                            "is_core_section": True,
                        },
                        {
                            "id": "section-2",
                            "title": "竞争格局",
                            "description": "比较主要厂商及其差异。",
                            "is_core_section": False,
                        },
                    ],
                },
                ensure_ascii=False,
            ),
        },
        state,
    )

    reasoning = [frame for frame in frames if frame["event_type"] == "chat.reasoning"]
    assert reasoning[-1]["content"] == (
        "### 行业分析\n\n"
        "规划思路：先分析市场，再比较竞争格局。\n\n"
        "1. **市场现状（重点）**\n"
        "   分析市场规模与增长驱动因素。\n"
        "2. **竞争格局**\n"
        "   比较主要厂商及其差异。"
    )
    assert '"sections"' not in reasoning[-1]["content"]
    assert '"thought"' not in reasoning[-1]["content"]


def test_plan_reasoning_json_is_rendered_as_readable_markdown():
    frames = route_chunk(
        {
            "agent": "plan_reasoning",
            "section_idx": "1",
            "section_title": "市场现状",
            "event": "message",
            "content": json.dumps(
                {
                    "id": "plan-1",
                    "language": "zh-CN",
                    "title": "梳理市场现状",
                    "thought": "先确认规模，再定位增长因素。",
                    "is_research_completed": False,
                    "steps": [
                        {
                            "id": "step-1",
                            "type": "info_collecting",
                            "title": "收集市场规模数据",
                            "description": "查找近三年的市场规模和增速。",
                        },
                        {
                            "id": "step-2",
                            "type": "info_collecting",
                            "title": "识别增长驱动因素",
                            "description": "汇总政策、需求和技术变化。",
                        },
                    ],
                },
                ensure_ascii=False,
            ),
        },
        RouterState(),
    )

    reasoning = [frame for frame in frames if frame["event_type"] == "chat.reasoning"]
    assert reasoning[-1]["content"] == (
        "#### 调研计划：梳理市场现状\n\n"
        "调研思路：先确认规模，再定位增长因素。\n\n"
        "状态：继续调研\n\n"
        "1. **收集市场规模数据**\n"
        "   查找近三年的市场规模和增速。\n"
        "2. **识别增长驱动因素**\n"
        "   汇总政策、需求和技术变化。"
    )


def test_retrieved_source_json_is_rendered_as_markdown_link():
    frames = route_chunk(
        {
            "agent": "collector_info_retrieval",
            "section_idx": "1",
            "section_title": "市场现状",
            "event": "summary_response",
            "content": json.dumps(
                {
                    "title": "2026 市场研究[摘要]",
                    "url": "https://example.com/report",
                    "query": "2026 市场规模 增长率",
                },
                ensure_ascii=False,
            ),
        },
        RouterState(),
    )

    reasoning = [frame for frame in frames if frame["event_type"] == "chat.reasoning"]
    assert reasoning[-1]["content"] == (
        "发现资料：[2026 市场研究\\[摘要\\]](<https://example.com/report>)\n\n"
        "检索词：2026 市场规模 增长率"
    )


def test_retrieved_source_does_not_link_non_http_url():
    frames = route_chunk(
        {
            "agent": "collector_info_retrieval",
            "section_idx": "1",
            "section_title": "市场现状",
            "event": "summary_response",
            "content": json.dumps(
                {
                    "title": "外部来源",
                    "url": "javascript:alert(1)",
                    "query": "市场规模",
                },
                ensure_ascii=False,
            ),
        },
        RouterState(),
    )

    reasoning = [frame for frame in frames if frame["event_type"] == "chat.reasoning"]
    assert reasoning[-1]["content"] == (
        "发现资料：外部来源\n\n"
        "链接：javascript\\:alert\\(1\\)\n\n"
        "检索词：市场规模"
    )
    assert "](" not in reasoning[-1]["content"]


def test_unknown_json_and_plain_text_keep_original_content():
    state = RouterState()
    unknown = route_chunk(
        {
            "agent": "collector_supervisor",
            "section_idx": "1",
            "section_title": "市场现状",
            "event": "message",
            "content": '{"custom":"value"}',
        },
        state,
    )
    plain = route_chunk(
        {
            "agent": "collector_summary",
            "section_idx": "1",
            "section_title": "市场现状",
            "event": "summary_response",
            "content": "资料已经覆盖市场规模和增速。",
        },
        state,
    )

    unknown_reasoning = [frame for frame in unknown if frame["event_type"] == "chat.reasoning"]
    plain_reasoning = [frame for frame in plain if frame["event_type"] == "chat.reasoning"]
    assert unknown_reasoning[-1]["content"] == '{"custom":"value"}'
    assert plain_reasoning[-1]["content"] == "资料已经覆盖市场规模和增速。"


def test_target_nodes_keep_incomplete_json_unchanged():
    cases = [
        ("outline", '{"title":"not-an-outline","custom":"value"}'),
        ("plan_reasoning", '{"title":"not-a-plan","custom":"value"}'),
        ("collector_info_retrieval", '{"title":"not-a-source","custom":"value"}'),
    ]

    for agent, raw_content in cases:
        frames = route_chunk(
            {
                "agent": agent,
                "section_idx": "1",
                "section_title": "市场现状",
                "event": "message",
                "content": raw_content,
            },
            RouterState(),
        )

        reasoning = [frame for frame in frames if frame["event_type"] == "chat.reasoning"]
        assert reasoning[-1]["content"] == raw_content


def test_outline_display_fields_cannot_create_links_or_raw_html():
    frames = route_chunk(
        {
            "agent": "outline",
            "event": "message",
            "content": json.dumps(
                {
                    "title": '[mail](mailto:test@example.com)<a href="https://evil.example">x</a>',
                    "thought": "[internal](/models)",
                    "sections": [
                        {
                            "title": "[section](https://evil.example)",
                            "description": '<a href="https://evil.example">危险</a>',
                        }
                    ],
                },
                ensure_ascii=False,
            ),
        },
        RouterState(),
    )

    reasoning = [frame for frame in frames if frame["event_type"] == "chat.reasoning"]
    content = reasoning[-1]["content"]
    assert "](" not in content
    assert r"\[mail\]\(mailto" in content
    assert r"\<a href" in content


def test_retrieved_source_allows_only_constructed_http_link():
    frames = route_chunk(
        {
            "agent": "collector_info_retrieval",
            "section_idx": "1",
            "section_title": "市场现状",
            "event": "summary_response",
            "content": json.dumps(
                {
                    "title": "[mail](mailto:test@example.com)",
                    "url": "https://example.com/report",
                    "query": '[internal](/models)<a href="https://evil.example">x</a>',
                },
                ensure_ascii=False,
            ),
        },
        RouterState(),
    )

    reasoning = [frame for frame in frames if frame["event_type"] == "chat.reasoning"]
    content = reasoning[-1]["content"]
    assert content.count("](") == 1
    assert r"\[mail\]\(mailto" in content
    assert r"\[internal\]\(\/models\)" in content
    assert r"\<a href" in content


def test_format_outline_card_markdown_happy_path():
    data = {
        "title": "行业分析",
        "thought": "先分析市场，再比较竞争格局。",
        "sections": [
            {"title": "市场现状", "is_core_section": True},
            {"title": "竞争格局", "is_core_section": False},
            {"title": "发展趋势", "is_core_section": True},
        ],
    }
    result = _format_outline_card_markdown(data)
    assert result is not None
    assert "## 页面规划" in result
    assert "### P1: 市场现状（重点）" in result
    assert "### P2: 竞争格局" in result
    assert "### P3: 发展趋势（重点）" in result
    assert "# 大纲：行业分析" in result
    assert "**研究思路**：先分析市场，再比较竞争格局。" in result


def test_format_outline_card_markdown_missing_title_returns_none():
    assert _format_outline_card_markdown({}) is None
    assert _format_outline_card_markdown({"title": "", "sections": []}) is None
    assert _format_outline_card_markdown({"title": "t", "sections": []}) is None
    assert _format_outline_card_markdown({"title": "t", "sections": None}) is None


def test_format_outline_card_markdown_empty_sections_returns_none():
    data = {"title": "测试", "sections": [{"title": ""}]}
    assert _format_outline_card_markdown(data) is None


def test_format_outline_card_markdown_no_thought():
    data = {
        "title": "测试",
        "sections": [{"title": "第一章", "is_core_section": False}],
    }
    result = _format_outline_card_markdown(data)
    assert result is not None
    assert "**研究思路**" not in result
    assert "### P1: 第一章" in result


def test_user_input_ended_emits_chat_reasoning():
    state = RouterState()
    frames = route_chunk({"event": "user_input_ended"}, state)
    assert frames == [
        {
            "event_type": "chat.reasoning",
            "task_id": "deepresearch_stage_2",
            "stream_source_id": "dr_outline",
            "content": "大纲已根据您的修改重新生成，自动确认继续研究",
        },
    ]


def test_user_input_ended_does_not_mutate_state():
    state = RouterState()
    route_chunk({"event": "user_input_ended"}, state)
    assert state.active_nodes == {}
    assert state.interrupt_node_id == ""


def test_brief_outline_uses_stage_two_and_outline_preview_contract():
    state = RouterState(current_stage=1)
    outline = json.dumps(
        {
            "title": "智能家电竞品速览",
            "sections": [
                {"id": "1", "title": "市场格局"},
                {"id": "2", "title": "产品对比"},
            ],
        },
        ensure_ascii=False,
    )

    frames = route_chunk(
        {"agent": "brief_outline", "event": "start", "content": outline},
        state,
    )

    _assert_stage(_stage_update(frames), 2)
    assert "".join(state.outline_parts) == outline
    reasoning = _process_reasoning(frames)
    assert reasoning
    assert reasoning[-1]["task_id"] == "deepresearch_stage_2"
    assert reasoning[-1]["stream_source_id"] == "dr_outline"
    assert "市场格局" in reasoning[-1]["content"]


@pytest.mark.parametrize(
    "agent",
    [
        "brief_info_collector",
        "brief_evidence_reviewer",
        "brief_sub_reporter",
    ],
)
def test_brief_research_nodes_advance_stage_three_in_task_list(agent):
    frames = route_chunk(
        {"agent": agent, "event": "start", "content": "处理中"},
        RouterState(current_stage=2),
    )

    _assert_stage(_stage_update(frames), 3)
    assert all(frame["event_type"] != "chat.delta" for frame in frames)


def test_brief_sub_reporter_completion_starts_stage_four_before_final_node_chunk():
    state = RouterState(current_stage=3)

    frames = route_chunk(
        {"agent": "brief_sub_reporter", "event": "done", "content": "章节写作完成"},
        state,
    )

    assert state.final_report_started is True
    _assert_stage(_stage_update(frames), 4)
    node_complete = next(
        index
        for index, frame in enumerate(frames)
        if frame["event_type"] == "chat.reasoning"
        and frame.get("task_id") == "deepresearch_stage_3"
        and frame.get("stream_source_id") == "dr_brief_sub_reporter"
        and frame.get("content") == "精简章节撰写完成\n"
    )
    stage_four = next(
        index
        for index, frame in enumerate(frames)
        if frame["event_type"] == "task.update"
        and frame["tasks"][3]["status"] == "in_progress"
    )
    assert node_complete < stage_four


@pytest.mark.parametrize(
    "agent",
    [
        "brief_reporter",
        "brief_mermaid_generator",
        "brief_source_tracer",
        "brief_html_reporter",
    ],
)
def test_brief_final_nodes_start_stage_four_without_professional_sections(agent):
    state = RouterState(current_stage=3)

    frames = route_chunk(
        {"agent": agent, "event": "start", "content": "处理中"},
        state,
    )

    assert state.final_report_started is True
    _assert_stage(_stage_update(frames), 4)
    final_frames = [
        frame
        for frame in frames
        if frame.get("stream_source_id") == "deepresearch_final_report"
    ]
    assert final_frames[0]["event_type"] == "task.start"
    assert any(
        frame["event_type"] == "chat.reasoning"
        for frame in final_frames
    )


@pytest.mark.parametrize(
    ("agent", "start_message"),
    [
        ("brief_info_collector", "报告级资料检索开始\n"),
        ("brief_evidence_reviewer", "证据审阅开始\n"),
    ],
)
def test_brief_process_nodes_preserve_reasoning_and_content(agent, start_message):
    frames = route_chunk(
        {
            "agent": agent,
            "event": "message",
            "reasoning_content": "正在判断证据覆盖范围",
            "content": "证据详情：Redis 适合共享状态，SQLite 适合本地持久化。",
        },
        RouterState(current_stage=2),
    )

    reasoning = _process_reasoning(frames)
    assert [frame["content"] for frame in reasoning] == [
        start_message,
        "正在判断证据覆盖范围",
        "证据详情：Redis 适合共享状态，SQLite 适合本地持久化。",
    ]
    assert all(frame["task_id"] == "deepresearch_stage_3" for frame in reasoning)
    assert all(frame["stream_source_id"] == f"dr_{agent}" for frame in reasoning)
