# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Unit tests for subagent thinking adapter."""

from __future__ import annotations

import pytest

from jiuwenswarm.common.thinking.adapter import adapt_thinking
from jiuwenswarm.common.thinking.types import normalize_thinking
from jiuwenswarm.common.thinking.vendor_map import match_vendor_style, style_to_kwargs


class TestNormalizeThinking:
    def test_empty_and_default(self):
        assert normalize_thinking("") == ("default", False)
        assert normalize_thinking("default") == ("default", False)
        assert normalize_thinking(None) == ("default", False)

    def test_off_on_case_insensitive(self):
        assert normalize_thinking("OFF") == ("off", False)
        assert normalize_thinking(" On ") == ("on", False)

    def test_invalid(self):
        value, invalid = normalize_thinking("low")
        assert value == "default"
        assert invalid is True


class TestVendorMap:
    def test_match_glm_allowlist(self):
        assert match_vendor_style("glm-5") == "extra_body_thinking_type"
        assert match_vendor_style("GLM-5.1") == "extra_body_thinking_type"
        assert match_vendor_style("glm-5.2") == "extra_body_thinking_type"
        assert match_vendor_style("provider/glm_5.1") == "extra_body_thinking_type"

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
        assert match_vendor_style("glm-5.3") is None
        # 词边界：无分隔的前缀拼名不命中
        assert match_vendor_style("somedeepseek") is None
        assert match_vendor_style("") is None

    def test_style_kwargs(self):
        off = style_to_kwargs("extra_body_thinking_type", enabled=False)
        assert off["extra_body"]["thinking"]["type"] == "disabled"
        on = style_to_kwargs("extra_body_enable_thinking", enabled=True)
        assert on["extra_body"]["enable_thinking"] is True


class TestAdaptThinking:
    def test_default_no_inject(self):
        profile = adapt_thinking("", model_name="glm-5")
        assert profile.thinking == "default"
        assert profile.injected is False
        assert dict(profile.llm_call_kwargs) == {}
        assert profile.model_name == "glm-5"

    def test_off_glm(self):
        profile = adapt_thinking("off", model_name="glm-5.1")
        assert profile.injected is True
        assert profile.degraded is False
        assert profile.llm_call_kwargs["extra_body"]["thinking"]["type"] == "disabled"

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
        profile = adapt_thinking("high", model_name="glm-5")
        assert profile.thinking == "default"
        assert profile.degraded is True
        assert profile.reason == "invalid_value"

    def test_profile_nested_freeze(self):
        profile = adapt_thinking("off", model_name="glm-5")
        # Nested mappings are frozen; assignment must raise.
        with pytest.raises(TypeError):
            profile.llm_call_kwargs["extra_body"]["thinking"]["type"] = "enabled"
        assert profile.llm_call_kwargs["extra_body"]["thinking"]["type"] == "disabled"
