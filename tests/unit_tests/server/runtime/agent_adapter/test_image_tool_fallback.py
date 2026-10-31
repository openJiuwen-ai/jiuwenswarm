# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""图像工具回退 notice：按本次模型能力触发，image_count 取视图引用数。"""

from __future__ import annotations

from types import SimpleNamespace

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)


class _FakeModel:
    def __init__(self, model_name: str, modalities: list[str] | None):
        self.model_config = SimpleNamespace(model_name=model_name)
        self.modalities = modalities


def _request(**params: object) -> AgentRequest:
    return AgentRequest(request_id="req-1", params=params)


def _query_with_refs(*paths: str) -> str:
    lines = []
    for index, path in enumerate(paths, start=1):
        lines.append(
            f"[image #{index}] path={path} filename={path.rsplit('/', 1)[-1]} mime=image/png"
        )
    return "对比图片\n" + "\n".join(lines)


def test_model_declares_image_no_notice():
    model = _FakeModel("gpt-4o", modalities=["text", "image"])
    notice = JiuWenSwarmDeepAdapter._build_image_tool_fallback_notice(
        _request(query="对比"),
        enable_read_image_multimodal=True,
        model=model,
        query=_query_with_refs("/a/one.png"),
    )
    assert notice is None


def test_text_only_model_emits_notice_with_view_ref_count():
    model = _FakeModel("deepseek-chat", modalities=["text"])
    notice = JiuWenSwarmDeepAdapter._build_image_tool_fallback_notice(
        _request(query="对比"),
        enable_read_image_multimodal=False,
        model=model,
        query=_query_with_refs("/a/one.png", "/a/two.png"),
    )
    assert notice is not None
    assert notice["notice_type"] == "image_tool_fallback"
    assert notice["image_count"] == 2


def test_explicit_false_overrides_image_capable_model():
    model = _FakeModel("gpt-4o", modalities=["text", "image"])
    notice = JiuWenSwarmDeepAdapter._build_image_tool_fallback_notice(
        _request(query="对比"),
        enable_read_image_multimodal=False,
        model=model,
        query=_query_with_refs("/a/one.png"),
    )
    assert notice is not None
    assert notice["image_count"] == 1


def test_global_switch_on_but_text_model_still_notice():
    # 触发条件按模型实际能力，不受全局 enable 开关单方面压制。
    model = _FakeModel("deepseek-chat", modalities=["text"])
    notice = JiuWenSwarmDeepAdapter._build_image_tool_fallback_notice(
        _request(query="对比"),
        enable_read_image_multimodal=True,
        model=model,
        query=_query_with_refs("/a/one.png"),
    )
    assert notice is not None

    # prompt 侧不再向 query 注入图片工具上下文：原生 vs VQA 的多模态策略
    # 由 MultimodalImageRail.before_model_call 按模型能力写入系统提示，
    # 这里保持纯文本模型仍发 notice（上面断言）且 query 原样透传。
    inputs = {
        "query": "对比图片",
        "_multimodal_image_files": [
            {"path": "/a/one.png", "filename": "one.png", "mime_type": "image/png"},
        ],
    }
    result = JiuWenSwarmDeepAdapter._prepare_react_image_tool_prompt(
        _request(query="对比图片"),
        inputs,
        enable_read_image_multimodal=True,
        model=model,
    )
    assert result is inputs
    assert result["query"] == "对比图片"


def test_no_image_references_no_notice():
    model = _FakeModel("deepseek-chat", modalities=["text"])
    notice = JiuWenSwarmDeepAdapter._build_image_tool_fallback_notice(
        _request(query="对比"),
        enable_read_image_multimodal=False,
        model=model,
        query="对比图片",
    )
    assert notice is None