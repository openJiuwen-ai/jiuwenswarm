# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""三态 enable_read_image_multimodal 解析：显式强制 vs 模型能力回退。"""

from __future__ import annotations

from types import SimpleNamespace

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)


class _FakeModel:
    """贴合真实 Model：model_config.model_name + 实例属性 modalities。"""

    def __init__(self, model_name: str, modalities: list[str] | None):
        self.model_config = SimpleNamespace(model_name=model_name)
        self.modalities = modalities


def test_resolve_explicit_true_forces_on():
    assert JiuWenSwarmDeepAdapter._resolve_enable_read_image_multimodal(
        {"enable_read_image_multimodal": True}, None
    ) is True


def test_resolve_explicit_false_forces_off():
    assert JiuWenSwarmDeepAdapter._resolve_enable_read_image_multimodal(
        {"enable_read_image_multimodal": False}, None
    ) is False


def test_resolve_unset_falls_back_to_model():
    model = _FakeModel("gpt-4o", modalities=["text", "image"])
    assert JiuWenSwarmDeepAdapter._resolve_enable_read_image_multimodal({}, model) is True


def test_resolve_unset_unknown_model_false():
    model = _FakeModel("deepseek-chat", modalities=["text"])
    assert JiuWenSwarmDeepAdapter._resolve_enable_read_image_multimodal({}, model) is False
