# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import pytest

from jiuwenswarm.common.model_vendor_registry import PlanKind, get_preset, to_frontend_payload


def test_frontend_payload_contains_protocol_scoped_reasoning_capabilities() -> None:
    payload = to_frontend_payload()
    alibaba = next(item for item in payload["token_plan"] if item["vendor_key"] == "alibaba")
    qwen = alibaba["reasoning_capabilities"]["qwen3.8-max"]

    assert qwen["openai"] == {
        "options": ["off", "low", "medium", "xhigh"],
        "recommended": "xhigh",
    }
    assert qwen["anthropic"] == {
        "options": ["off", "low", "medium", "xhigh"],
        "recommended": "xhigh",
    }


def test_frontend_payload_isolates_single_model_capability_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 单个模型的能力查询异常不能拖垮整个 vendors.list：该模型条目被跳过，
    # 其余模型与厂商卡片照常返回（前端回落到 reasoning_rules/model_fallbacks）。
    import jiuwenswarm.common.model_vendor_registry as registry

    real_capability = registry.get_reasoning_capability

    def _flaky(*args: object, **kwargs: object):
        if kwargs.get("model") == "qwen3.8-max":
            raise RuntimeError("boom")
        return real_capability(*args, **kwargs)

    monkeypatch.setattr(registry, "get_reasoning_capability", _flaky)
    payload = registry.to_frontend_payload()
    alibaba = next(item for item in payload["token_plan"] if item["vendor_key"] == "alibaba")

    assert "qwen3.8-max" not in alibaba["reasoning_capabilities"]
    assert alibaba["reasoning_capabilities"]


def test_frontend_payload_contains_provider_scoped_reasoning_rules() -> None:
    payload = to_frontend_payload()
    qianfan = next(item for item in payload["custom_api"] if item["vendor_key"] == "baidu")
    glm52 = next(rule for rule in qianfan["reasoning_rules"] if "glm-5.2*" in rule["patterns"])

    # Qianfan proxies GLM with a plain toggle; the cross-vendor fallback for
    # glm-5.2 exposes off/high/max instead. The preset rule must win so the
    # frontend matches the backend save validation.
    assert glm52["capabilities"]["openai"]["options"] == ["off", "on"]


def test_frontend_payload_contains_custom_model_fallbacks() -> None:
    reasoning = to_frontend_payload()["reasoning"]
    glm = next(item for item in reasoning["model_fallbacks"] if "glm-5.2*" in item["patterns"])

    assert reasoning["protocol_defaults"]["openai"]["options"] == ["off", "low", "medium", "high"]
    assert glm["capabilities"]["openai"]["options"] == ["off", "high", "max"]


def test_modelarts_presets_use_current_v2_model_ids() -> None:
    token_plan = get_preset("maas", PlanKind.TOKEN_PLAN)
    custom_api = get_preset("maas", PlanKind.CUSTOM_API)

    assert token_plan is not None
    assert token_plan.model_options == ("glm-5.1", "kimi-k2.6", "deepseek-v4-flash")
    assert custom_api is not None
    assert custom_api.default_model == "openpangu-2.0-pro"
    assert "pangu-large" not in custom_api.model_options


def test_alibaba_custom_api_uses_curated_verified_model_allowlist() -> None:
    preset = get_preset("alibaba", PlanKind.CUSTOM_API)

    assert preset is not None
    assert preset.default_model == "qwen3.8-max"
    assert len(preset.model_options) == 47
    assert {
        "qwen3.8-max",
        "qwen3-coder-next",
        "deepseek-r1",
        "deepseek-v3.2",
        "deepseek-v4-pro",
        "deepseek-v4-pro-0813",
        "kimi-k2-thinking",
        "kimi-k2.6",
        "MiniMax-M2.1",
        "MiniMax-M2.5",
        "glm-4.7",
        "glm-5.2-fast-preview",
        "qwen3-vl-plus",
        "qwen3.5-omni-plus",
        "qwen3.8-27b",
        "qwen-mt-plus",
        "qwen-deep-search-planning",
    } <= set(preset.model_options)
    assert "qwen-turbo-0919" not in preset.model_options
    assert all("/" not in model_id for model_id in preset.model_options)
