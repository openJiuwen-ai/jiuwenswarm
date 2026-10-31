# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""三态 enable_read_image_multimodal 解析：模型条目声明是硬边界，显式配置分层兜底。"""

from __future__ import annotations

from types import SimpleNamespace

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)


class _FakeModel:
    """贴合真实 Model：model_config.model_name + 实例属性 modalities / supports_image。"""

    def __init__(
        self,
        model_name: str,
        modalities: list[str] | None,
        supports_image: bool | None = None,
    ):
        self.model_config = SimpleNamespace(model_name=model_name)
        self.modalities = modalities
        self.supports_image = supports_image


def test_resolve_explicit_true_forces_on_for_unknown_model():
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


def test_explicit_true_cannot_override_text_only_model_entry():
    """显式 true 不能越过模型条目的「明确不支持」——否则请求必然失败。"""
    model = _FakeModel("custom-text-model", modalities=["text"])
    assert JiuWenSwarmDeepAdapter._resolve_enable_read_image_multimodal(
        {"enable_read_image_multimodal": True}, model
    ) is False


def test_supports_image_false_entry_blocks_explicit_true():
    model = _FakeModel("gpt-4o", modalities=None, supports_image=False)
    assert JiuWenSwarmDeepAdapter._resolve_enable_read_image_multimodal(
        {"enable_read_image_multimodal": True}, model
    ) is False


def test_explicit_false_can_disable_image_capable_model_entry():
    """显式 false 是用户主动降级 VQA 的合法开关，模型声明支持也照关。"""
    model = _FakeModel("gpt-4o", modalities=["text", "image"])
    assert JiuWenSwarmDeepAdapter._resolve_enable_read_image_multimodal(
        {"enable_read_image_multimodal": False}, model
    ) is False


def test_unset_entry_falls_back_to_builtin_table():
    model = _FakeModel("gpt-4o", modalities=None)
    assert JiuWenSwarmDeepAdapter._resolve_enable_read_image_multimodal({}, model) is True
    model = _FakeModel("o3-mini", modalities=None)
    assert JiuWenSwarmDeepAdapter._resolve_enable_read_image_multimodal({}, model) is False
