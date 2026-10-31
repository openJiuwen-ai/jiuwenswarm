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


def test_forced_native_path_does_not_claim_tool_fallback():
    # 解析结果 True 即走原生图片输入（像素由 rail 物化），不能再提示「已切换为工具」。
    model = _FakeModel("deepseek-chat", modalities=["text"])
    notice = JiuWenSwarmDeepAdapter._build_image_tool_fallback_notice(
        _request(query="对比"),
        enable_read_image_multimodal=True,
        model=model,
        query=_query_with_refs("/a/one.png"),
    )
    assert notice is None


def test_no_image_references_no_notice():
    model = _FakeModel("deepseek-chat", modalities=["text"])
    notice = JiuWenSwarmDeepAdapter._build_image_tool_fallback_notice(
        _request(query="对比"),
        enable_read_image_multimodal=False,
        model=model,
        query="对比图片",
    )
    assert notice is None