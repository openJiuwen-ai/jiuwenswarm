# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Enterprise model template → config.yaml apply (retry → anomaly rail)."""

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


def test_sync_writes_retry_count_zero() -> None:
    """retry_count=0 须写入（防 falsy 漏写），并强制开启已禁用的 rail。"""
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
    assert rail["enabled"] is True
    assert rail["transient_max_retries"] == 0
    assert rail["max_retries"] == 2
    assert base["execution_guard"]["model_anomaly_detection_rail"]["transient_max_retries"] == 3


def test_sync_skips_when_retry_count_absent() -> None:
    """缺省 retry_count 不覆盖 yaml 已有 rail 值。"""
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
    assert rail["transient_max_retries"] == 7


def test_sync_creates_rail_when_execution_guard_missing() -> None:
    """execution_guard 缺失时新建段、写入 retries，并 enabled=True。"""
    base: dict = {"models": {}}
    merged, applied = apply_enterprise_models_to_config(
        base, _enterprise_with_default_model(retry_count=3)
    )
    assert applied is True
    rail = merged["execution_guard"]["model_anomaly_detection_rail"]
    assert rail == {"enabled": True, "transient_max_retries": 3}
    assert "execution_guard" not in base
