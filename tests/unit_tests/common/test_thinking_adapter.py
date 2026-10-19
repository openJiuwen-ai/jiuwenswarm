# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Unit tests for subagent thinking adapter."""

from __future__ import annotations

import pytest

from jiuwenswarm.common.thinking.adapter import adapt_thinking
from jiuwenswarm.common.thinking.types import normalize_thinking
from jiuwenswarm.common.thinking.vendor_map import (
    DEFAULT_THINKING_DEPTH,
    depth_to_kwargs,
    match_vendor_style,
    style_to_kwargs,
)


class TestNormalizeThinking:
    def test_empty_and_default(self):
        assert normalize_thinking("") == ("default", False)
        assert normalize_thinking("default") == ("default", False)
        assert normalize_thinking(None) == ("default", False)

    def test_off_on_case_insensitive(self):
        assert normalize_thinking("OFF") == ("off", False)
        assert normalize_thinking(" On ") == ("on", False)

    def test_depth_tiers(self):
        assert normalize_thinking("low") == ("low", False)
        assert normalize_thinking("MEDIUM") == ("medium", False)
        assert normalize_thinking("high") == ("high", False)

    def test_invalid(self):
        value, invalid = normalize_thinking("xhigh")
        assert value == "default"
        assert invalid is True


class TestVendorMap:
    def test_match_glm_allowlist(self):
        assert match_vendor_style("glm-5") == "extra_body_thinking_type"
        assert match_vendor_style("GLM-5.1") == "extra_body_thinking_type"
        assert match_vendor_style("glm-5.2") == "extra_body_thinking_type"
        assert match_vendor_style("provider/glm_5.1") == "extra_body_thinking_type"

    def test_match_glm53_effort_style(self):
        assert match_vendor_style("glm-5.3") == "glm53_reasoning_effort"
        assert match_vendor_style("glm-5.3-test") == "glm53_reasoning_effort"
        assert match_vendor_style("GLM-5.3") == "glm53_reasoning_effort"

    def test_match_deepseek_family(self):
        assert match_vendor_style("DeepSeek-V3.2") == "extra_body_thinking_type"
        assert match_vendor_style("deepseek-v3.2") == "extra_body_thinking_type"
        assert match_vendor_style("deepseek_v3.2") == "extra_body_thinking_type"
        assert match_vendor_style("deepseek-v4-flash-0731") == "extra_body_thinking_type"
        assert match_vendor_style("deepseek-v4-pro") == "extra_body_thinking_type"
        assert match_vendor_style("deepseek-chat") == "extra_body_thinking_type"
        assert match_vendor_style("provider/deepseek-v3.1") == "extra_body_thinking_type"

    def test_unsupported(self):
        assert match_vendor_style("doubao-pro") is None
        assert match_vendor_style("qwen3-max") is None
        assert match_vendor_style("glm-4") is None
        assert match_vendor_style("glm-50") is None
        # 词边界：无分隔的前缀拼名不命中
        assert match_vendor_style("somedeepseek") is None
        assert match_vendor_style("") is None

    def test_style_kwargs_legacy(self):
        off = style_to_kwargs("extra_body_thinking_type", enabled=False)
        assert off["extra_body"]["thinking"]["type"] == "disabled"
        on = style_to_kwargs("extra_body_enable_thinking", enabled=True)
        assert on["extra_body"]["enable_thinking"] is True

    def test_glm53_depth_never_disables(self):
        off = depth_to_kwargs("glm53_reasoning_effort", "off")
        assert off["extra_body"]["thinking"]["type"] == "enabled"
        assert off["reasoning_effort"] == "low"
        low = depth_to_kwargs("glm53_reasoning_effort", "low")
        assert low["reasoning_effort"] == "low"
        medium = depth_to_kwargs("glm53_reasoning_effort", "medium")
        assert medium["reasoning_effort"] == "high"
        high = depth_to_kwargs("glm53_reasoning_effort", "high")
        assert high["reasoning_effort"] == "max"

    def test_thinking_type_low_includes_effort(self):
        low = depth_to_kwargs("extra_body_thinking_type", "low")
        assert low["extra_body"]["thinking"]["type"] == "enabled"
        assert low["reasoning_effort"] == "low"


class TestAdaptThinking:
    def test_default_no_inject_for_non_glm53(self):
        profile = adapt_thinking("", model_name="glm-5")
        assert profile.thinking == "default"
        assert profile.injected is False
        assert dict(profile.llm_call_kwargs) == {}
        assert profile.model_name == "glm-5"

        deepseek = adapt_thinking("default", model_name="deepseek-v4-flash-0731")
        assert deepseek.injected is False
        assert deepseek.thinking == "default"

    def test_default_glm53_uses_effort_low(self):
        assert DEFAULT_THINKING_DEPTH == "low"
        profile = adapt_thinking("default", model_name="glm-5.3-test")
        assert profile.injected is True
        assert profile.vendor_style == "glm53_reasoning_effort"
        assert profile.thinking == "low"
        assert profile.llm_call_kwargs["extra_body"]["thinking"]["type"] == "enabled"
        assert profile.llm_call_kwargs["reasoning_effort"] == "low"

        bare = adapt_thinking("", model_name="glm-5.3")
        assert bare.injected is True
        assert bare.llm_call_kwargs["reasoning_effort"] == "low"

    def test_off_glm(self):
        profile = adapt_thinking("off", model_name="glm-5.1")
        assert profile.injected is True
        assert profile.degraded is False
        assert profile.llm_call_kwargs["extra_body"]["thinking"]["type"] == "disabled"

    def test_off_glm53_falls_back_to_low(self):
        profile = adapt_thinking("off", model_name="glm-5.3")
        assert profile.injected is True
        assert profile.llm_call_kwargs["extra_body"]["thinking"]["type"] == "enabled"
        assert profile.llm_call_kwargs["reasoning_effort"] == "low"

    def test_on_deepseek_v32(self):
        profile = adapt_thinking("on", model_name="DeepSeek-V3.2")
        assert profile.injected is True
        assert profile.llm_call_kwargs["extra_body"]["thinking"]["type"] == "enabled"

    def test_off_deepseek_v4_flash(self):
        profile = adapt_thinking("off", model_name="deepseek-v4-flash-0731")
        assert profile.injected is True
        assert profile.degraded is False
        assert profile.llm_call_kwargs["extra_body"]["thinking"]["type"] == "disabled"

    def test_unsupported_degraded(self):
        profile = adapt_thinking("off", model_name="qwen3-plus")
        assert profile.injected is False
        assert profile.degraded is True
        assert profile.reason == "unsupported_model"

    def test_invalid_degraded(self):
        profile = adapt_thinking("xhigh", model_name="glm-5")
        assert profile.thinking == "default"
        assert profile.degraded is True
        assert profile.reason == "invalid_value"
        assert profile.injected is False

    def test_profile_nested_freeze(self):
        profile = adapt_thinking("off", model_name="glm-5")
        # Nested mappings are frozen; assignment must raise.
        with pytest.raises(TypeError):
            profile.llm_call_kwargs["extra_body"]["thinking"]["type"] = "enabled"
        assert profile.llm_call_kwargs["extra_body"]["thinking"]["type"] == "disabled"
