# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""模型原生能力（modalities）的配置归一：只认 modalities 是否含 image（[[S_22]]）。"""

from __future__ import annotations

from jiuwenswarm.agents.harness.common.tools.model_capabilities import (
    coerce_modalities,
    supports_native_image,
)


def test_coerce_modalities_normalizes_yaml_literals():
    """modalities 可能是列表，也可能是逗号/空白分隔字符串；统一成小写去重列表。"""
    assert coerce_modalities(["text", "image"]) == ["text", "image"]
    assert coerce_modalities(("text", "image")) == ["text", "image"]
    assert coerce_modalities("text,image") == ["text", "image"]
    assert coerce_modalities(" text  image ") == ["text", "image"]
    assert coerce_modalities(["TEXT", "IMAGE", "text"]) == ["text", "image"]
    # 无法识别 / 空 → None（未声明），调用方按不支持处理。
    assert coerce_modalities("") is None
    assert coerce_modalities([]) is None
    assert coerce_modalities(123) is None
    assert coerce_modalities(None) is None


def test_supports_native_image_only_when_image_present():
    """只有 modalities 含 image 才支持原生看图；未声明/纯文本一律不支持。"""
    assert supports_native_image(["text", "image"]) is True
    assert supports_native_image("text,image") is True
    assert supports_native_image(["text"]) is False
    assert supports_native_image(None) is False
    assert supports_native_image("") is False