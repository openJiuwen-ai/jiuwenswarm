# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the auto-gear ``enable_routing`` request injection.

relay 的 auto 档在 ``params.model_name`` 上仍下发**保底具体模型名**，另置
``params.enable_routing=True`` 表达「本帧模型名可被路由覆盖」。adapter 侧把它写进
``inputs["run"]["context"]["extra"]["enable_routing"]``，供 ``ModelRoutingRail``
每请求读取（本阶段只打日志、不切换模型）。

关键约束：
- 严格判 ``is True``：缺省 / ``False`` / 非布尔（含字符串 "true"）一律**不写**，
  避免任何异常输入把 rail 从「跳过」翻成「真路由」。
- 与 ``model_selection`` 共用同一层 ``run_context.extra``，互不覆盖。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


def _request(params: object) -> SimpleNamespace:
    return SimpleNamespace(params=params)


def _extra_of(inputs: dict) -> dict:
    return inputs["run"]["context"]["extra"]


def test_inject_writes_flag_for_strict_true():
    inputs: dict = {}
    JiuWenSwarmDeepAdapter._inject_model_routing_into_inputs(
        _request({"enable_routing": True}), inputs
    )
    assert _extra_of(inputs)["enable_routing"] is True


@pytest.mark.parametrize("value", [False, None, "true", "True", 1, 0, "1", {}, []])
def test_inject_skips_every_non_boolean_true(value):
    inputs: dict = {}
    JiuWenSwarmDeepAdapter._inject_model_routing_into_inputs(
        _request({"enable_routing": value}), inputs
    )
    assert inputs == {}


def test_inject_skips_when_key_absent():
    inputs: dict = {}
    JiuWenSwarmDeepAdapter._inject_model_routing_into_inputs(_request({"model_name": "auto"}), inputs)
    assert inputs == {}


def test_inject_skips_non_dict_params():
    for params in (None, "enable_routing=true", ["enable_routing"], 1):
        inputs: dict = {}
        JiuWenSwarmDeepAdapter._inject_model_routing_into_inputs(_request(params), inputs)
        assert inputs == {}


def test_inject_coexists_with_model_selection():
    """两个键写进同一层 run_context.extra，互不覆盖。"""
    inputs: dict = {}
    request = _request({"model_name": "deepseek-v4.1-flash", "enable_routing": True})
    JiuWenSwarmDeepAdapter._inject_model_selection_into_inputs(request, inputs)
    JiuWenSwarmDeepAdapter._inject_model_routing_into_inputs(request, inputs)

    extra = _extra_of(inputs)
    assert extra["model_selection"] == "deepseek-v4.1-flash"
    assert extra["enable_routing"] is True


def test_inject_is_idempotent_and_preserves_other_extra_keys():
    inputs = {"run": {"context": {"extra": {"model_selection": "auto"}}}}
    request = _request({"enable_routing": True})
    JiuWenSwarmDeepAdapter._inject_model_routing_into_inputs(request, inputs)
    JiuWenSwarmDeepAdapter._inject_model_routing_into_inputs(request, inputs)

    extra = _extra_of(inputs)
    assert extra == {"model_selection": "auto", "enable_routing": True}
