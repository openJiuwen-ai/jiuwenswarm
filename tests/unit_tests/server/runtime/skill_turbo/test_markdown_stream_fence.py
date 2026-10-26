# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from jiuwenswarm.server.runtime.skill_turbo.markdown_stream import (
    markdown_stream_incoming,
    terminate_dangling_markdown_fence,
)


def test_glued_json_fences_get_a_separating_newline():
    previous = '{"material_richness":"empty"}\n```'
    incoming = '```json\n{"entity":"Beiersdorf"}'
    joined = previous + markdown_stream_incoming(previous, incoming)
    assert "``````json" not in joined
    assert "```\n```json" in joined


def test_progress_text_is_not_stuck_on_closing_fence():
    previous = "```"
    incoming = "未发现附件，已提取部分需求信息，后续将补充确认"
    joined = previous + markdown_stream_incoming(previous, incoming)
    assert "```未发现附件" not in joined
    assert "```\n未发现附件" in joined


def test_english_progress_text_is_not_stuck_on_closing_fence():
    previous = '{"step":"search"}\n```'
    incoming = "Processing results..."
    joined = previous + markdown_stream_incoming(previous, incoming)
    assert "```Processing" not in joined
    assert "```\nProcessing results..." in joined


def test_streaming_json_info_string_stays_on_fence_line():
    previous = "```"
    incoming = "json\n{"
    assert markdown_stream_incoming(previous, incoming) == incoming
    assert previous + incoming == "```json\n{"


def test_ordinary_json_tokens_stay_glued():
    previous = '{"topic": "'
    incoming = 'Beiersdorf 2025年度报告"}'
    assert markdown_stream_incoming(previous, incoming) == incoming


def test_terminate_completed_fence_but_not_marker_only():
    assert terminate_dangling_markdown_fence("```json") == "```json\n"
    assert terminate_dangling_markdown_fence("```") == "```"
    assert terminate_dangling_markdown_fence("hello") == "hello"
    assert terminate_dangling_markdown_fence("```\n") == "```\n"
