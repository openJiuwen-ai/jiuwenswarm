# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import pytest

from jiuwenswarm.common.reasoning_config import (
    effective_endpoint_profile,
    normalize_reasoning_level,
    reasoning_config_for_level,
    reasoning_level_options,
    resolve_endpoint_profile_override,
    validate_reasoning_level_for_model,
)
from jiuwenswarm.common.reasoning_injector import inject_reasoning_params
from openjiuwen.core.foundation.llm import get_reasoning_capability


def test_reasoning_level_is_forwarded_as_provider_neutral_core_config() -> None:
    enabled = inject_reasoning_params(
        model_client_config={"api_base": "https://api.deepseek.com", "model_name": "deepseek-v4-pro"},
        model_config_obj={"reasoning_level": "max", "temperature": 0.2},
    )
    disabled = inject_reasoning_params(
        model_client_config={"api_base": "https://api.deepseek.com", "model_name": "deepseek-v4-pro"},
        model_config_obj={"reasoning_level": "off"},
    )

    assert enabled == {
        "reasoning": {"mode": "enabled", "effort": "max"},
        "temperature": 0.2,
    }
    assert disabled == {"reasoning": {"mode": "disabled"}}


def test_reasoning_level_aliases_are_canonicalized() -> None:
    assert normalize_reasoning_level("enabled") == "on"
    assert normalize_reasoning_level("ultra") == "max"
    assert reasoning_config_for_level("on") == {"mode": "enabled"}


def test_reasoning_level_options_follow_core_capability() -> None:
    kimi = get_reasoning_capability(provider="Moonshot", model="kimi-k3", protocol="openai")
    minimax = get_reasoning_capability(provider="MiniMax", model="MiniMax-M3", protocol="openai")
    unsupported = get_reasoning_capability(provider="Zhipu", model="codegeex-4", protocol="openai")

    assert reasoning_level_options(kimi) == ("low", "high", "max")
    assert reasoning_level_options(minimax) == ("off", "on")
    assert reasoning_level_options(unsupported) == ()


_VLLM_GATEWAY_BASE = "http://10.0.0.1:8080/v1/"


@pytest.fixture()
def vllm_override_config(monkeypatch: pytest.MonkeyPatch) -> None:
    # 覆盖表不再内置任何 host，来源是用户 config.yaml 的
    # endpoint_profile_overrides；测试直接注入配置读取结果。
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_endpoint_profile_overrides",
        lambda config=None: {"10.0.0.1": "vllm"},
    )


def test_endpoint_profile_override_comes_from_user_config(vllm_override_config: None) -> None:
    assert resolve_endpoint_profile_override(_VLLM_GATEWAY_BASE) == "vllm"
    assert resolve_endpoint_profile_override("https://api.deepseek.com") is None

    # 显式配置的方言优先；空值/空白才走 host 推断。
    assert effective_endpoint_profile(_VLLM_GATEWAY_BASE, "deepseek") == "deepseek"
    assert effective_endpoint_profile(_VLLM_GATEWAY_BASE, "  ") == "vllm"
    assert effective_endpoint_profile(_VLLM_GATEWAY_BASE, None) == "vllm"


def test_get_endpoint_profile_overrides_normalizes_entries() -> None:
    from jiuwenswarm.common.config import get_endpoint_profile_overrides

    assert get_endpoint_profile_overrides({}) == {}
    assert get_endpoint_profile_overrides({"endpoint_profile_overrides": None}) == {}
    assert get_endpoint_profile_overrides(
        {"endpoint_profile_overrides": {" GW.Example.COM ": " vllm ", "": "x", "h": ""}}
    ) == {"gw.example.com": "vllm"}


def test_no_override_configured_means_no_inference(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_endpoint_profile_overrides",
        lambda config=None: {},
    )
    assert resolve_endpoint_profile_override(_VLLM_GATEWAY_BASE) is None
    assert effective_endpoint_profile(_VLLM_GATEWAY_BASE, None) is None


def test_validate_reasoning_level_uses_model_family_on_inferred_vllm_gateway(
    vllm_override_config: None,
) -> None:
    # 该网关只改线格式，不改档位表：GLM-5.2 仍是 off/high/max。
    assert (
        validate_reasoning_level_for_model(
            raw_level="high",
            model_name="GLM-5.2",
            model_provider="OpenAI",
            api_base=_VLLM_GATEWAY_BASE,
        )
        == "high"
    )
    assert (
        validate_reasoning_level_for_model(
            raw_level="max",
            model_name="GLM-5.2",
            model_provider="OpenAI",
            api_base=_VLLM_GATEWAY_BASE,
        )
        == "max"
    )
    with pytest.raises(ValueError, match="off, high, max"):
        validate_reasoning_level_for_model(
            raw_level="xhigh",
            model_name="GLM-5.2",
            model_provider="OpenAI",
            api_base=_VLLM_GATEWAY_BASE,
        )
    assert (
        validate_reasoning_level_for_model(
            raw_level="xhigh",
            model_name="qwen3.8-max",
            model_provider="OpenAI",
            api_base=_VLLM_GATEWAY_BASE,
        )
        == "xhigh"
    )


def test_validate_reasoning_level_normalizes_legacy_yaml_booleans() -> None:
    # legacy 配置里裸写的 off/on 会被 YAML 1.1 读成布尔。TUI model.update 会把
    # 存储值原样回传本函数：False 必须归一化为 "off" 而不是被当成"未设置"
    # 清空（否则用户显式关闭思考的配置会在改其它字段时被静默删除）。
    assert (
        validate_reasoning_level_for_model(
            raw_level=False,
            model_name="GLM-5.2",
            model_provider="OpenAI",
            api_base="https://example.com/v1",
        )
        == "off"
    )
    assert (
        validate_reasoning_level_for_model(
            raw_level=True,
            model_name="MiniMax-M3",
            model_provider="MiniMax",
            api_base="https://api.minimaxi.com/v1",
        )
        == "on"
    )
    # 仅 None/空白才视为"未设置"（沿用厂商默认）。
    for unset in (None, "", "   "):
        assert (
            validate_reasoning_level_for_model(
                raw_level=unset,
                model_name="GLM-5.2",
                model_provider="OpenAI",
                api_base="https://example.com/v1",
            )
            == ""
        )


def test_validate_reasoning_level_wraps_unexpected_capability_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 能力查询异常（如 swarm/core 版本漂移）应转成 ValueError 并保留原因，
    # 让保存路径返回 BAD_REQUEST 而不是 500。
    import openjiuwen.core.foundation.llm as core_llm

    def _boom(**_kwargs: object) -> None:
        raise RuntimeError("capability table unavailable")

    monkeypatch.setattr(core_llm, "get_reasoning_capability", _boom)
    with pytest.raises(ValueError, match="cannot be validated") as excinfo:
        validate_reasoning_level_for_model(
            raw_level="high",
            model_name="deepseek-v4-pro",
            model_provider="DeepSeek",
            api_base="https://api.deepseek.com",
        )
    assert isinstance(excinfo.value.__cause__, RuntimeError)
