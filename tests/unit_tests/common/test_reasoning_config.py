# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import pytest

from jiuwenswarm.common.reasoning_config import resolve_reasoning_target


@pytest.mark.parametrize("model", ["deepseek-v4.1-flash", "deepseek-v4.1-flash-1017", "DeepSeek-V4.1-Flash"])
def test_v4_1_models_resolve_on_direct_deepseek(model):
    assert resolve_reasoning_target(
        client_provider="openai",
        api_base="https://api.deepseek.com/v1",
        model_name=model,
    ) == ("deepseek_official", model.lower())


def test_v4_1_model_resolves_on_bailian():
    assert resolve_reasoning_target(
        client_provider="openai",
        api_base="https://dashscope.aliyuncs.com/compatible-mode/v1",
        model_name="deepseek-v4.1-flash",
    ) == ("dashscope_bailian", "deepseek-v4.1-flash")


def test_v4_1_model_stays_untouched_outside_direct_channels():
    assert resolve_reasoning_target(
        client_provider="openai",
        api_base="https://api.modelarts-maas.com/v1",
        model_name="deepseek-v4.1-flash",
    ) is None


def test_unlisted_model_stays_untouched_on_direct_channel():
    assert resolve_reasoning_target(
        client_provider="openai",
        api_base="https://api.deepseek.com/v1",
        model_name="deepseek-chat",
    ) is None
