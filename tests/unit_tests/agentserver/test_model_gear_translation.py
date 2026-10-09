# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""档位关键字（fast/balanced/extreme/auto）必须在构建 Model 的唯一入口翻成具体模型。

背景：档位关键字有两条通道。单 agent 主链路走 request param，由 ModelRoutingRail
翻译；另一条经 env（OFFICE_CLAW_EFFECTIVE_MODEL → MODEL_NAME → config.yaml
models.defaults）直接流进 ``_build_model_from_entry`` 构建 Model。没有 rail 的消费方
——子代理（deep_agent 直接继承 ``_deep_config.model``，且 ``subagent_rails=None``
不继承父 rail）、code 模式、DeepResearch 隔离子进程——会把裸档位当模型名直送 MaaS，
得到 ``ModelArts.81009 Invalid model``。
"""

from __future__ import annotations

import pytest

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


def _entry(model_name: str) -> dict:
    return {
        "is_default": True,
        "model_client_config": {
            "model_name": model_name,
            "client_provider": "OpenAI",
            "api_base": "https://maas.example/v2/",
            "api_key": "k",
        },
        "model_config_obj": {},
    }


def _wire_model_name(model) -> str:
    """实际发往 MaaS 的模型名（ModelRequestConfig.model_name，alias="model"）。"""
    return str(getattr(model.model_config, "model_name", "") or "")


@pytest.mark.parametrize(
    ("gear", "concrete"),
    [
        ("extreme", "glm-5.2"),
        ("fast", "deepseek-v4.1-flash"),
        ("balanced", "deepseek-v4.1-flash"),
        ("auto", "deepseek-v4.1-flash"),
    ],
)
def test_build_model_from_entry_translates_gear_for_wire_name(gear: str, concrete: str) -> None:
    mcc = _entry(gear)["model_client_config"]
    model = JiuWenSwarmDeepAdapter._build_model_from_entry(mcc, {})

    assert _wire_model_name(model) == concrete
    # mcc 不被改写：cache key / identity 仍按档位关键字登记，relay 按档位选择不受影响。
    assert mcc["model_name"] == gear


@pytest.mark.parametrize("concrete", ["glm-5.2", "deepseek-v4.1-flash", "gpt-4"])
def test_build_model_from_entry_passes_through_concrete_model(concrete: str) -> None:
    model = JiuWenSwarmDeepAdapter._build_model_from_entry(_entry(concrete)["model_client_config"], {})

    assert _wire_model_name(model) == concrete


def test_subagent_resolving_by_gear_gets_concrete_wire_model() -> None:
    """子代理按档位关键字解析时，拿到的 Model 必须发具体模型 id（81009 修复点）。"""
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._model_cache = {}
    adapter._model_name_to_keys = {}
    adapter._model_identity_to_keys = {}
    adapter._model_identity_owners = {}
    adapter._tier_model_cache = {}
    adapter._model = None
    adapter._instance = None

    adapter._register_model_cache_entry(_entry("extreme"), {})

    # 档位关键字仍是合法查找键（relay 按档位下发 model_name / model_ref 时命中）。
    assert "extreme" in adapter._model_cache
    # 真实链路里 _create_model 一定会给出默认 Model；子代理解析入口要求它非空。
    adapter._model = adapter._model_cache["extreme"]

    model, _fallback = adapter._resolve_model_for_subagent(model_name="extreme")

    assert _wire_model_name(model) == "glm-5.2"
