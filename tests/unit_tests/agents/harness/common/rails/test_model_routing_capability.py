# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""_build_cap_from_entry 的能力声明（modalities）透传。"""

from __future__ import annotations

from types import SimpleNamespace

from jiuwenswarm.agents.harness.common.rails.model_routing.capability import (
    _build_cap_from_entry,
)


def test_build_cap_passes_modalities_to_three_arg_builder() -> None:
    """视觉候选经能力表构建后，其 Model 必须携带 modalities（含 image）。

    否则 sync_agent_image_capability 会把它判成"不支持"，视觉候选经自动路由后
    能力位被写成 False（CR-17 的镜像缺陷）。
    """

    captured = {}

    def builder(mcc, mco, modalities=None):
        captured["modalities"] = modalities
        return SimpleNamespace(modalities=modalities)

    entry = {
        "model_client_config": {"model_name": "vision-x", "api_base": "https://v"},
        "model_config_obj": {},
        "modalities": ["text", "image"],
    }
    cap = _build_cap_from_entry(entry, builder)

    assert captured["modalities"] == ["text", "image"]
    assert cap.model.modalities == ["text", "image"]


def test_build_cap_falls_back_to_two_arg_builder() -> None:
    """2 参旧 builder（无能力位参数）不应因三参调用而崩：TypeError 兜底退回 2 参。"""

    def builder(mcc, mco):
        return SimpleNamespace()

    entry = {
        "model_client_config": {"model_name": "glm-5.3", "api_base": "https://v"},
        "model_config_obj": {},
        "modalities": ["text"],
    }
    cap = _build_cap_from_entry(entry, builder)

    assert cap.model is not None
