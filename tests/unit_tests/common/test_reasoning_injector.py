# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""reasoning_injector：厂商思考字段透传与 reasoning_level 注入边界。"""

from __future__ import annotations

from jiuwenswarm.common.reasoning_injector import (
    build_reasoning_model_request_kwargs,
    inject_reasoning_params,
)

_DEEPSEEK_OFFICIAL = {
    "client_provider": "openai",
    "api_base": "https://api.deepseek.com/v1",
    "model_name": "deepseek-v4.1-flash",
}
_BAILIAN = {
    "client_provider": "openai",
    "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "model_name": "deepseek-v4.1-flash",
}
_MAAS = {
    "client_provider": "openai",
    "api_base": "https://api.modelarts-maas.com/v1",
    "model_name": "glm-5.3",
}


def test_officeace_passthrough_keeps_vendor_fields_byte_for_byte():
    """OfficeAce catalog：只有厂商字段时原样进入请求 kwargs。"""
    model_config_obj = {
        "extra_body": {"thinking": {"type": "enabled"}},
        "reasoning_effort": "high",
    }
    result = build_reasoning_model_request_kwargs(
        model_client_config=_MAAS,
        model_config_obj=model_config_obj,
        model_name="glm-5.3",
    )
    assert result["model"] == "glm-5.3"
    assert result["extra_body"] == {"thinking": {"type": "enabled"}}
    assert result["reasoning_effort"] == "high"
    assert "reasoning_level" not in result


def test_explicit_fields_with_reasoning_level_are_not_rewritten_on_whitelist():
    """并存时：显式厂商字段优先，reasoning_level 不得改写。"""
    model_config_obj = {
        "reasoning_level": "off",
        "extra_body": {"thinking": {"type": "enabled"}},
        "reasoning_effort": "high",
    }
    result = inject_reasoning_params(
        model_client_config=_DEEPSEEK_OFFICIAL,
        model_config_obj=model_config_obj,
    )
    assert result["extra_body"] == {"thinking": {"type": "enabled"}}
    assert result["reasoning_effort"] == "high"
    assert "reasoning_level" not in result


def test_explicit_enable_thinking_false_blocks_reasoning_level_rewrite():
    """extra_body.enable_thinking=False 也是显式字段，必须保住。"""
    model_config_obj = {
        "reasoning_level": "high",
        "extra_body": {"enable_thinking": False},
    }
    result = inject_reasoning_params(
        model_client_config=_BAILIAN,
        model_config_obj=model_config_obj,
    )
    assert result["extra_body"] == {"enable_thinking": False}
    assert "reasoning_effort" not in result
    assert "reasoning_level" not in result


def test_legacy_reasoning_level_only_still_injects_on_deepseek_official():
    """仅 reasoning_level：白名单端点上旧注入行为不变。"""
    result = inject_reasoning_params(
        model_client_config=_DEEPSEEK_OFFICIAL,
        model_config_obj={"reasoning_level": "high"},
    )
    assert result["extra_body"]["thinking"] == {"type": "enabled"}
    assert result["reasoning_effort"] == "high"
    assert "reasoning_level" not in result


def test_legacy_reasoning_level_only_still_injects_on_bailian():
    result = inject_reasoning_params(
        model_client_config=_BAILIAN,
        model_config_obj={"reasoning_level": "off"},
    )
    assert result["extra_body"]["enable_thinking"] is False
    assert "reasoning_effort" not in result
    assert "reasoning_level" not in result


def test_legacy_reasoning_level_on_maas_stays_passthrough_without_inject():
    """华为 MaaS 不在 reasoning 白名单：只剥离 reasoning_level，不注入。"""
    model_config_obj = {
        "reasoning_level": "high",
        "temperature": 0.2,
    }
    result = inject_reasoning_params(
        model_client_config=_MAAS,
        model_config_obj=model_config_obj,
    )
    assert result == {"temperature": 0.2}


def test_no_thinking_fields_and_no_reasoning_level_unchanged():
    model_config_obj = {"temperature": 0.7, "max_tokens": 1024}
    result = build_reasoning_model_request_kwargs(
        model_client_config=_MAAS,
        model_config_obj=model_config_obj,
        model_name="glm-5.3",
    )
    assert result["model"] == "glm-5.3"
    assert result["temperature"] == 0.7
    assert result["max_tokens"] == 1024
    assert "extra_body" not in result
    assert "reasoning_effort" not in result
