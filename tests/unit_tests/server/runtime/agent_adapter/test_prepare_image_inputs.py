# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""上传进图写引用行：_prepare_multimodal_image_inputs 把引用行追加进 query。"""

from __future__ import annotations

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)


def _request(**params: object) -> AgentRequest:
    return AgentRequest(request_id="req-1", params=params)


def _media_item(path: str, mime_type: str = "image/png", **extra: object) -> dict:
    return {"path": path, "mime_type": mime_type, **extra}


def test_upload_writes_reference_line_into_query():
    adapter = JiuWenSwarmDeepAdapter()
    request = _request(query="对比", media_items=[_media_item("/a/home.png")])
    inputs = adapter._prepare_multimodal_image_inputs(request, {"query": "对比"})
    assert "[image #1]" in inputs["query"]
    assert "path=/a/home.png" in inputs["query"]


def test_no_image_is_noop():
    adapter = JiuWenSwarmDeepAdapter()
    request = _request(query="对比")
    inputs = adapter._prepare_multimodal_image_inputs(request, {"query": "对比"})
    assert inputs == {"query": "对比"}


def test_multiple_images_increment_index():
    adapter = JiuWenSwarmDeepAdapter()
    request = _request(
        query="对比",
        media_items=[_media_item("/a/one.png"), _media_item("/a/two.png")],
    )
    inputs = adapter._prepare_multimodal_image_inputs(request, {"query": "对比"})
    assert "[image #1]" in inputs["query"]
    assert "[image #2]" in inputs["query"]
    assert "path=/a/one.png" in inputs["query"]
    assert "path=/a/two.png" in inputs["query"]


def test_same_path_not_written_twice():
    adapter = JiuWenSwarmDeepAdapter()
    first = adapter._prepare_multimodal_image_inputs(
        _request(query="q1", media_items=[_media_item("/a/home.png")]),
        {"query": "q1"},
    )
    assert first["query"].count("[image #") == 1

    second = adapter._prepare_multimodal_image_inputs(
        _request(query="q2", media_items=[_media_item("/a/home.png")]),
        {"query": "q2"},
    )
    assert second["query"] == "q2"


def test_new_path_after_same_path_continues_index():
    adapter = JiuWenSwarmDeepAdapter()
    first = adapter._prepare_multimodal_image_inputs(
        _request(query="q1", media_items=[_media_item("/a/home.png")]),
        {"query": "q1"},
    )
    assert "[image #1]" in first["query"]

    second = adapter._prepare_multimodal_image_inputs(
        _request(query="q2", media_items=[_media_item("/a/other.png")]),
        {"query": "q2"},
    )
    assert "[image #2]" in second["query"]
    assert "path=/a/other.png" in second["query"]


def test_non_string_query_is_untouched():
    adapter = JiuWenSwarmDeepAdapter()
    request = _request(query="对比", media_items=[_media_item("/a/home.png")])
    inputs = adapter._prepare_multimodal_image_inputs(request, {"query": None})
    assert inputs["query"] is None
    assert "_multimodal_image_files" in inputs


def test_reference_line_has_no_base64():
    adapter = JiuWenSwarmDeepAdapter()
    request = _request(query="对比", media_items=[_media_item("/a/home.png")])
    inputs = adapter._prepare_multimodal_image_inputs(request, {"query": "对比"})
    assert "data:image/" not in inputs["query"]
    assert "base64" not in inputs["query"]