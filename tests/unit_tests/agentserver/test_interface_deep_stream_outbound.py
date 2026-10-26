# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Outbound stream: outer-tool_result resets streamed-content flag; task_id is forwarded."""

from types import SimpleNamespace

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
    _is_outer_react_tool_result,
    _propagate_stream_source_id,
)


def _answer_chunk(text: str = "以下是完成情况概要") -> SimpleNamespace:
    return SimpleNamespace(
        type="answer",
        payload={"output": {"output": text, "chunked": False}},
    )


def test_outer_skill_acceleration_tool_result_is_outer() -> None:
    assert _is_outer_react_tool_result(
        {
            "tool_result": {
                "tool_name": "skill_acceleration_exec",
                "tool_call_id": "call_outer_1",
                "result": "ok",
            }
        }
    )


def test_inner_skill_turbo_tool_id_is_not_outer() -> None:
    assert not _is_outer_react_tool_result(
        {
            "tool_call_id": "BashTool_skill_turbo",
            "result": "ok",
        }
    )


def test_inner_stream_source_id_is_not_outer() -> None:
    assert not _is_outer_react_tool_result(
        {
            "stream_source_id": "p6_1_page_worker",
            "tool_call_id": "call_page_1",
        }
    )


def test_nested_tool_result_skill_turbo_suffix_is_not_outer() -> None:
    assert not _is_outer_react_tool_result(
        {
            "tool_result": {
                "tool_id": "ReadFileTool_skill_turbo",
                "result": "{}",
            }
        }
    )


def test_empty_payload_counts_as_outer() -> None:
    assert _is_outer_react_tool_result({})
    assert _is_outer_react_tool_result(None)


def test_propagate_copies_task_id_and_stream_source_id() -> None:
    result = {"event_type": "chat.delta", "content": "完成执行 Stage 5: 模板上下文预处理（5/14）"}
    out = _propagate_stream_source_id(
        {
            "content": result["content"],
            "task_id": "task_abc123",
            "stream_source_id": "p6_1_page_worker",
        },
        result,
    )
    assert out["task_id"] == "task_abc123"
    assert out["stream_source_id"] == "p6_1_page_worker"


def test_propagate_skips_blank_task_id() -> None:
    result = {"event_type": "chat.delta", "content": "hello"}
    out = _propagate_stream_source_id({"task_id": "  "}, result)
    assert "task_id" not in out


def test_parse_llm_output_forwards_task_id() -> None:
    chunk = SimpleNamespace(
        type="llm_output",
        payload={
            "content": "完成执行 Stage 5: 模板上下文预处理（5/14）",
            "task_id": "task_abc123",
        },
    )
    parsed = JiuWenSwarmDeepAdapter._parse_stream_chunk(chunk)
    assert parsed["event_type"] == "chat.delta"
    assert parsed["content"] == "完成执行 Stage 5: 模板上下文预处理（5/14）"
    assert parsed["task_id"] == "task_abc123"


def test_parse_llm_reasoning_forwards_task_id() -> None:
    chunk = SimpleNamespace(
        type="llm_reasoning",
        payload={"content": "内部推理", "task_id": "task_think"},
    )
    parsed = JiuWenSwarmDeepAdapter._parse_stream_chunk(chunk)
    assert parsed["event_type"] == "chat.reasoning"
    assert parsed["task_id"] == "task_think"


def test_parse_llm_output_without_task_id_unchanged() -> None:
    chunk = SimpleNamespace(type="llm_output", payload={"content": "hello"})
    parsed = JiuWenSwarmDeepAdapter._parse_stream_chunk(chunk)
    assert parsed == {"event_type": "chat.delta", "content": "hello"}


def test_parse_llm_output_accepts_openjiuwen_output_key() -> None:
    """openjiuwen llm_controller streams payload.output, not payload.content.

    Before this fix, mid-ReAct user text was dropped while reasoning (which
    already accepted ``output``) still reached the thinking UI.
    """
    chunk = SimpleNamespace(
        type="llm_output",
        payload={"output": "我来帮你完成这个任务", "result_type": "answer"},
    )
    parsed = JiuWenSwarmDeepAdapter._parse_stream_chunk(chunk)
    assert parsed == {
        "event_type": "chat.delta",
        "content": "我来帮你完成这个任务",
    }


def test_parse_llm_output_prefers_content_over_output() -> None:
    chunk = SimpleNamespace(
        type="llm_output",
        payload={"content": "from-content", "output": "from-output"},
    )
    parsed = JiuWenSwarmDeepAdapter._parse_stream_chunk(chunk)
    assert parsed["content"] == "from-content"


def test_parse_content_chunk_forwards_task_id() -> None:
    chunk = SimpleNamespace(
        type="content_chunk",
        payload={"content": "开始执行 Stage 6: 内容策划（6/14）", "task_id": "task_plan"},
    )
    parsed = JiuWenSwarmDeepAdapter._parse_stream_chunk(chunk)
    assert parsed["event_type"] == "chat.delta"
    assert parsed["task_id"] == "task_plan"


def test_parse_chat_file_obs_url_is_not_dropped() -> None:
    """Enterprise send_file writes OutputSchema(type=chat.file, files=[url]).

    Fallback treats unknown typed chunks as chat.delta via content/output; a
    files-only payload would return None and Gateway would never materialize.
    """
    chunk = SimpleNamespace(
        type="chat.file",
        payload={
            "files": [
                {
                    "url": "http://minio-headless.default:9000/b/downloads/out.docx",
                    "name": "out.docx",
                    "size": 10,
                }
            ]
        },
    )
    parsed = JiuWenSwarmDeepAdapter._parse_stream_chunk(chunk)
    assert parsed is not None
    assert parsed["event_type"] == "chat.file"
    assert parsed["files"][0]["url"].startswith("http://")
    assert parsed["files"][0]["name"] == "out.docx"


def test_parse_unknown_chat_delta_payload_forwards_task_id() -> None:
    chunk = SimpleNamespace(
        type="node.progress",
        payload={
            "event_type": "chat.delta",
            "content": "完成执行 Stage 8: 深度研究（8/14）",
            "task_id": "task_research",
        },
    )
    parsed = JiuWenSwarmDeepAdapter._parse_stream_chunk(chunk)
    assert parsed["event_type"] == "chat.delta"
    assert parsed["task_id"] == "task_research"


def test_same_round_streamed_answer_still_empty_final() -> None:
    answer = "以下是完成情况概要"
    parsed = JiuWenSwarmDeepAdapter._parse_stream_chunk(
        _answer_chunk(answer),
        _has_streamed_content=True,
        _streamed_text=answer,
    )
    assert parsed == {"event_type": "chat.final", "content": ""}


def test_hitl_suppress_noise_keeps_flags() -> None:
    """Pause-tail metadata must not clear suppress.

    ``__interaction__`` is not noise: it is the ask_user card source after the
    forced-emit revert and must be forwarded.
    """
    for chunk_type in (
        "llm_usage",
        "context.usage",
        "controller_output",
    ):
        assert JiuWenSwarmDeepAdapter._is_hitl_suppress_noise_chunk(
            SimpleNamespace(type=chunk_type, payload={})
        )
    assert not JiuWenSwarmDeepAdapter._is_hitl_suppress_noise_chunk(
        SimpleNamespace(type="__interaction__", payload={})
    )


def test_hitl_suppress_task_failed_not_noise() -> None:
    """controller_output.task_failed must clear suppress and surface chat.error."""
    chunk = SimpleNamespace(
        type="controller_output",
        payload=SimpleNamespace(type="task_failed", data=[]),
    )
    assert not JiuWenSwarmDeepAdapter._is_hitl_suppress_noise_chunk(chunk)


def test_hitl_suppress_dict_task_failed_not_noise() -> None:
    """dict payload task_failed must also clear suppress (align stream_utils)."""
    chunk = SimpleNamespace(
        type="controller_output",
        payload={"type": "task_failed", "data": [{"text": "model failed"}]},
    )
    assert not JiuWenSwarmDeepAdapter._is_hitl_suppress_noise_chunk(chunk)
    assert JiuWenSwarmDeepAdapter._run_failure(chunk) == (
        "task_failed",
        "model failed",
    )
    parsed = JiuWenSwarmDeepAdapter._parse_stream_chunk(chunk)
    assert parsed == {"event_type": "chat.error", "error": "model failed"}


def test_controller_output_task_interaction_is_dropped() -> None:
    """ISSUE #3892: TASK_INTERACTION must not stringify as chat.delta body."""
    chunk = SimpleNamespace(
        type="controller_output",
        payload=SimpleNamespace(
            type="task_interaction",
            data=[SimpleNamespace(data={"result_type": "interrupt", "state": []})],
            metadata={"task_id": "t1"},
        ),
    )
    parsed = JiuWenSwarmDeepAdapter._parse_stream_chunk(chunk)
    assert parsed is None


def test_controller_output_control_plane_types_are_dropped() -> None:
    for inner in ("task_completion", "processing", "all_tasks_processed"):
        chunk = SimpleNamespace(
            type="controller_output",
            payload=SimpleNamespace(type=inner, data=[]),
        )
        assert JiuWenSwarmDeepAdapter._parse_stream_chunk(chunk) is None


def test_controller_output_dict_task_interaction_is_dropped() -> None:
    chunk = SimpleNamespace(
        type="controller_output",
        payload={
            "type": "task_interaction",
            "data": [
                {
                    "data": {
                        "result_type": "interrupt",
                        "state": [],
                        "interrupt_ids": ["call_x"],
                        "_interaction_emitted": True,
                    }
                }
            ],
            "metadata": {"task_id": "t2"},
        },
    )
    assert JiuWenSwarmDeepAdapter._parse_stream_chunk(chunk) is None


def test_controller_output_unknown_type_is_dropped() -> None:
    chunk = SimpleNamespace(
        type="controller_output",
        payload=SimpleNamespace(type="future_new_type", data=[]),
    )
    assert JiuWenSwarmDeepAdapter._parse_stream_chunk(chunk) is None


def test_hitl_suppress_cleared_on_resume_or_unknown_chunk() -> None:
    """Content and unknown SDK frames clear suppress (default = resumed)."""
    for chunk_type in ("llm_output", "answer", "chat.file", "task.start", "tool_call"):
        assert not JiuWenSwarmDeepAdapter._is_hitl_suppress_noise_chunk(
            SimpleNamespace(type=chunk_type, payload={})
        )
    assert not JiuWenSwarmDeepAdapter._is_hitl_suppress_noise_chunk(
        SimpleNamespace(payload={})
    )


def test_is_ask_user_payload_detects_ask_user() -> None:
    assert JiuWenSwarmDeepAdapter._is_ask_user_payload(
        {"event_type": "chat.ask_user_question", "questions": []}
    )


def test_is_ask_user_payload_rejects_non_ask_user() -> None:
    assert not JiuWenSwarmDeepAdapter._is_ask_user_payload(
        {"event_type": "chat.delta", "content": "hi"}
    )
    assert not JiuWenSwarmDeepAdapter._is_ask_user_payload(None)


def test_streamed_flag_without_visible_text_keeps_final_for_drain() -> None:
    answer = "以下是完成情况概要"
    parsed = JiuWenSwarmDeepAdapter._parse_stream_chunk(
        _answer_chunk(answer),
        _has_streamed_content=True,
        _streamed_text="",
    )
    assert parsed == {"event_type": "chat.final", "content": answer}


def test_after_outer_tool_result_reset_answer_keeps_summary() -> None:
    has_streamed_content = True
    if _is_outer_react_tool_result(
        {"tool_result": {"tool_name": "skill_acceleration_exec", "tool_call_id": "call_1"}}
    ):
        has_streamed_content = False
    parsed = JiuWenSwarmDeepAdapter._parse_stream_chunk(
        _answer_chunk(),
        _has_streamed_content=has_streamed_content,
    )
    assert parsed == {
        "event_type": "chat.final",
        "content": "以下是完成情况概要",
    }


def test_after_inner_tool_result_answer_stays_empty_final() -> None:
    answer = "以下是完成情况概要"
    has_streamed_content = True
    if _is_outer_react_tool_result({"tool_call_id": "BashTool_skill_turbo"}):
        has_streamed_content = False
    parsed = JiuWenSwarmDeepAdapter._parse_stream_chunk(
        _answer_chunk(answer),
        _has_streamed_content=has_streamed_content,
        _streamed_text=answer,
    )
    assert parsed == {"event_type": "chat.final", "content": ""}
