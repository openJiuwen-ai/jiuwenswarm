# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Enterprise model template → config apply.

模型 ``retry_count`` 不再写入执行护栏，也不再强制打开模型异常检测。
"""

from __future__ import annotations

from jiuwenswarm.enterprise.config.effective.apply_models import (
    apply_enterprise_models_to_config,
)
from jiuwenswarm.enterprise.config.effective.schemas import (
    EffectiveEnterpriseConfig,
    RoutingContext,
    TemplateRefSlot,
)


def _routing() -> RoutingContext:
    return RoutingContext(group_id="g1", bot_id="b1", user_id="u1")


def _enterprise_with_default_model(*, retry_count: int | None = 3) -> EffectiveEnterpriseConfig:
    entity: dict = {
        "template_name": "default",
        "template_id": "tpl-m1",
        "api_base": "https://example.com",
        "api_key": "sk-test",
        "model_id": "gpt-test",
        "model_provider": "OpenAI",
        "parameters": {},
    }
    if retry_count is not None:
        entity["retry_count"] = retry_count
    return EffectiveEnterpriseConfig(
        routing=_routing(),
        resource_id="b1",
        ref_template_id="tpl-1",
        models={TemplateRefSlot.DEFAULT_MODEL.value: [entity]},
    )


def test_model_retry_does_not_change_anomaly_rail() -> None:
    """retry_count 只留在模型客户端兼容字段，不改护栏开关和重试次数。"""
    base = {
        "models": {},
        "execution_guard": {
            "model_anomaly_detection_rail": {
                "enabled": False,
                "transient_max_retries": 3,
                "max_retries": 2,
            },
        },
    }
    merged, applied = apply_enterprise_models_to_config(
        base, _enterprise_with_default_model(retry_count=0)
    )
    assert applied is True
    rail = merged["execution_guard"]["model_anomaly_detection_rail"]
    assert rail["enabled"] is False
    assert rail["transient_max_retries"] == 3
    assert rail["max_retries"] == 2
    assert merged["models"]["default"]["model_client_config"]["max_retries"] == 0
    assert "transient_max_retries" not in merged["models"]["default"]
    assert base["execution_guard"]["model_anomaly_detection_rail"]["transient_max_retries"] == 3


def test_model_apply_keeps_yaml_rail_when_retry_count_absent() -> None:
    """缺省 retry_count 时 yaml 护栏原样保留。"""
    base = {
        "models": {},
        "execution_guard": {
            "model_anomaly_detection_rail": {
                "enabled": True,
                "transient_max_retries": 7,
            },
        },
    }
    merged, applied = apply_enterprise_models_to_config(
        base, _enterprise_with_default_model(retry_count=None)
    )
    assert applied is True
    rail = merged["execution_guard"]["model_anomaly_detection_rail"]
    assert rail["enabled"] is True
    assert rail["transient_max_retries"] == 7
    assert "max_retries" not in merged["models"]["default"]["model_client_config"]


def test_model_apply_does_not_create_execution_guard() -> None:
    """没有 yaml 护栏段时，模型套用也不新建。"""
    base: dict = {"models": {}}
    merged, applied = apply_enterprise_models_to_config(
        base, _enterprise_with_default_model(retry_count=3)
    )
    assert applied is True
    assert "execution_guard" not in merged
    assert "execution_guard" not in base
