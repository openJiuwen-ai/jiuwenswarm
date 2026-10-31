# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""模型图像能力判定：条目声明优先、内置表兜底、宽前缀误判收窄。"""

from __future__ import annotations

from jiuwenswarm.agents.harness.common.tools.model_capabilities import (
    builtin_model_supports_image,
    resolve_model_entry_support,
    resolve_model_supports_image,
)


def test_explicit_modalities_true():
    assert resolve_model_supports_image(["text", "image"]) is True


def test_explicit_modalities_text_only():
    assert resolve_model_supports_image(["text"]) is False


def test_unset_modalities_defaults_false():
    assert resolve_model_supports_image(None) is False


def test_empty_modalities_false():
    assert resolve_model_supports_image([]) is False


def test_supports_image_bool_wins_over_modalities():
    assert resolve_model_supports_image(["text"], "gpt-4o", True) is True
    assert resolve_model_supports_image(["text", "image"], "gpt-4o", False) is False


def test_entry_support_unset_returns_none():
    assert resolve_model_entry_support(None, None) is None


def test_entry_support_distinguishes_false_and_unknown():
    # 空 modalities / false 是「明确声明不支持」，None 才是「未声明」。
    assert resolve_model_entry_support([], None) is False
    assert resolve_model_entry_support(None, False) is False
    assert resolve_model_entry_support(None, True) is True


def test_builtin_table_marks_known_vision_families():
    assert builtin_model_supports_image("qwen3-vl-plus") is True
    assert builtin_model_supports_image("gpt-4o-mini") is True
    assert builtin_model_supports_image("claude-sonnet-4-20250514") is True


def test_builtin_table_no_longer_matches_text_only_variants():
    # 宽前缀历史误判：o1 系不支持图像；o3-mini / o3-pro 同为纯文本。
    assert builtin_model_supports_image("o1") is False
    assert builtin_model_supports_image("o1-mini") is False
    assert builtin_model_supports_image("o3-mini") is False
    assert builtin_model_supports_image("o3-pro") is False
    # llama-3.2 只有 11B / 90B 视觉变体；mistral-small-3.0 也是纯文本。
    assert builtin_model_supports_image("llama-3.2-11b-vision-instruct") is True
    assert builtin_model_supports_image("llama-3.2-1b-instruct") is False
    assert builtin_model_supports_image("llama-3.2-3b-instruct") is False
    assert builtin_model_supports_image("mistral-small-3.1-24b-instruct") is True
    assert builtin_model_supports_image("mistral-small-3") is False
    assert builtin_model_supports_image("gemini-1.0-pro") is False
    assert builtin_model_supports_image("gemini-2.5-flash") is True
