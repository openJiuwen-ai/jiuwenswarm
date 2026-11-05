# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""换模后看图能力位同步（interface_deep 与自动路由共用入口）。"""

from __future__ import annotations

from types import SimpleNamespace

from jiuwenswarm.agents.harness.common.rails.model_routing.image_capability_sync import (
    sync_agent_image_capability,
)


def _model(modalities) -> SimpleNamespace:
    return SimpleNamespace(modalities=modalities)


def _host(enabled: bool) -> SimpleNamespace:
    return SimpleNamespace(deep_config=SimpleNamespace(enable_read_image_multimodal=enabled))


def test_sync_sets_false_when_model_declares_no_image_support() -> None:
    """入口视觉模型切到文本模型后，能力位必须回落 False（否则 rail 误注入像素）。"""

    host = _host(enabled=True)

    assert sync_agent_image_capability(host, _model(["text"])) is True
    assert host.deep_config.enable_read_image_multimodal is False


def test_sync_keeps_true_for_vision_model() -> None:
    host = _host(enabled=False)

    assert sync_agent_image_capability(host, _model(["text", "image"])) is True
    assert host.deep_config.enable_read_image_multimodal is True


def test_sync_treats_unset_support_as_unsupported() -> None:
    """自动路由构造的新模型未携带 modalities 时按不支持处理（对文本目标正确）。"""

    host = _host(enabled=True)

    assert sync_agent_image_capability(host, _model(None)) is True
    assert host.deep_config.enable_read_image_multimodal is False


def test_sync_noop_without_deep_config() -> None:
    assert sync_agent_image_capability(SimpleNamespace(), _model(["text", "image"])) is False
