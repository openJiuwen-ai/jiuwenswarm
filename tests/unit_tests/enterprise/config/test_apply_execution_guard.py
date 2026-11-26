# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.

"""企业执行护栏：未绑定保留 yaml，合法 body 整段替换，非法 body 回落且不写文件。"""

from __future__ import annotations

import copy
import logging
from pathlib import Path

import pytest

from jiuwenswarm.enterprise.config.config_provider import (
    AgentConfigContext,
    ConfigSource,
    DefaultConfigProvider,
    _apply_enterprise_policy,
)
from jiuwenswarm.enterprise.config.effective.apply_execution_guard import (
    apply_enterprise_execution_guard_to_config,
)
from jiuwenswarm.enterprise.config.effective.execution_guard_body import (
    DEFAULT_EXECUTION_GUARD_BODY,
)
from jiuwenswarm.enterprise.config.effective.schemas import (
    DEFAULT_AGENT_LOAD_SLOTS,
    EffectiveEnterpriseConfig,
    RoutingContext,
    TemplateRefSlot,
)
from jiuwenswarm.enterprise.config import config_provider


def _routing() -> RoutingContext:
    return RoutingContext(group_id="g1", bot_id="b1", user_id="u1")


def _enterprise(*, execution_guard) -> EffectiveEnterpriseConfig:
    return EffectiveEnterpriseConfig(
        routing=_routing(),
        resource_id="b1",
        execution_guard=execution_guard,
    )


def _yaml_guard() -> dict:
    return {
        "model_anomaly_detection_rail": {"enabled": False, "transient_max_retries": 1},
        "circuit_breaker": {"enabled": True, "warning_threshold": 2},
    }


def test_default_load_slots_include_execution_guard() -> None:
    assert "execution_guard" in DEFAULT_AGENT_LOAD_SLOTS


def test_unbound_keeps_yaml() -> None:
    base = {"execution_guard": _yaml_guard()}
    merged, applied = apply_enterprise_execution_guard_to_config(
        base, _enterprise(execution_guard=None)
    )
    assert applied is False
    assert merged is base
    assert merged["execution_guard"]["model_anomaly_detection_rail"]["transient_max_retries"] == 1


def test_valid_body_replaces_execution_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    writes: list[str] = []

    def _refuse_write(self, *args, **kwargs):
        writes.append("write")
        raise AssertionError("execution guard apply must not write files")

    monkeypatch.setattr(Path, "write_text", _refuse_write)
    monkeypatch.setattr(Path, "write_bytes", _refuse_write)
    base = {"execution_guard": _yaml_guard(), "models": {"default": {"keep": True}}}
    body = copy.deepcopy(DEFAULT_EXECUTION_GUARD_BODY)
    body["model_anomaly_detection_rail"]["transient_max_retries"] = 9
    body["circuit_breaker"]["enabled"] = True
    merged, applied = apply_enterprise_execution_guard_to_config(
        base,
        _enterprise(execution_guard=[{"template_id": "g1", "enabled": True, "body": body}]),
    )
    assert applied is True
    assert writes == []
    assert merged["execution_guard"]["model_anomaly_detection_rail"]["transient_max_retries"] == 9
    assert merged["execution_guard"]["circuit_breaker"]["enabled"] is True
    assert base["execution_guard"]["circuit_breaker"]["warning_threshold"] == 2
    assert merged["execution_guard"]["circuit_breaker"]["warning_threshold"] == 10
    assert merged["models"] == {"default": {"keep": True}}
    assert base["execution_guard"]["model_anomaly_detection_rail"]["transient_max_retries"] == 1


def test_invalid_body_keeps_yaml_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    base = {"execution_guard": _yaml_guard()}
    body = copy.deepcopy(DEFAULT_EXECUTION_GUARD_BODY)
    body["model_anomaly_detection_rail"]["backoff_seconds"] = [1]
    with caplog.at_level(logging.ERROR):
        merged, applied = apply_enterprise_execution_guard_to_config(
            base,
            _enterprise(execution_guard=[{"template_id": "g1", "enabled": True, "body": body}]),
        )
    assert applied is False
    assert merged is base
    assert merged["execution_guard"]["model_anomaly_detection_rail"]["transient_max_retries"] == 1
    assert "invalid body" in caplog.text


def test_disabled_row_keeps_yaml() -> None:
    base = {"execution_guard": _yaml_guard()}
    merged, applied = apply_enterprise_execution_guard_to_config(
        base,
        _enterprise(
            execution_guard=[{
                "template_id": "g1",
                "enabled": False,
                "body": copy.deepcopy(DEFAULT_EXECUTION_GUARD_BODY),
            }]
        ),
    )
    assert applied is False
    assert merged["execution_guard"]["circuit_breaker"]["enabled"] is True


def test_multiple_entities_use_first_and_warn(caplog: pytest.LogCaptureFixture) -> None:
    first = copy.deepcopy(DEFAULT_EXECUTION_GUARD_BODY)
    first["model_anomaly_detection_rail"]["transient_max_retries"] = 4
    second = copy.deepcopy(DEFAULT_EXECUTION_GUARD_BODY)
    second["model_anomaly_detection_rail"]["transient_max_retries"] = 8
    with caplog.at_level(logging.WARNING):
        merged, applied = apply_enterprise_execution_guard_to_config(
            {"execution_guard": _yaml_guard()},
            _enterprise(execution_guard=[
                {"template_id": "first", "enabled": True, "body": first},
                {"template_id": "second", "enabled": True, "body": second},
            ]),
        )
    assert applied is True
    assert merged["execution_guard"]["model_anomaly_detection_rail"]["transient_max_retries"] == 4
    assert "first" in caplog.text and "second" in caplog.text


def test_guard_body_wins_after_model_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    """先套模型、后套护栏时，护栏 body 的开关和重试次数不被模型 retry_count 盖掉。"""

    def _passthrough_memory(merged):
        return merged

    def _noop_credentials(_loaded):
        return None

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.memory.config.merge_memory_config_into_config",
        _passthrough_memory,
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.mcp_dynamic_token_patch.apply_dispatched_credentials",
        _noop_credentials,
    )

    yaml_guard = copy.deepcopy(DEFAULT_EXECUTION_GUARD_BODY)
    yaml_guard["model_anomaly_detection_rail"]["enabled"] = False
    yaml_guard["model_anomaly_detection_rail"]["transient_max_retries"] = 1
    body = copy.deepcopy(DEFAULT_EXECUTION_GUARD_BODY)
    body["model_anomaly_detection_rail"]["enabled"] = True
    body["model_anomaly_detection_rail"]["transient_max_retries"] = 9
    loaded = EffectiveEnterpriseConfig(
        routing=_routing(),
        resource_id="b1",
        models={
            TemplateRefSlot.DEFAULT_MODEL.value: [{
                "template_name": "default",
                "template_id": "tpl-m1",
                "api_base": "https://example.com",
                "api_key": "sk-test",
                "model_id": "gpt-test",
                "model_provider": "OpenAI",
                "parameters": {},
                "retry_count": 0,
            }]
        },
        execution_guard=[{"template_id": "g1", "enabled": True, "body": body}],
    )
    out = _apply_enterprise_policy(
        {"models": {}, "execution_guard": yaml_guard},
        loaded,
    )
    rail = out["execution_guard"]["model_anomaly_detection_rail"]
    assert rail["enabled"] is True
    assert rail["transient_max_retries"] == 9
    assert out["models"]["default"]["model_client_config"]["max_retries"] == 0


@pytest.mark.asyncio
async def test_yaml_source_does_not_apply_enterprise(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def _boom(base, loaded):
        nonlocal called
        called = True
        return base

    monkeypatch.setattr(config_provider, "resolve_config_source", lambda: ConfigSource.YAML)
    monkeypatch.setattr(config_provider, "_apply_enterprise_policy", _boom)
    result = await DefaultConfigProvider().load_agent_config(
        AgentConfigContext(),
        base={"execution_guard": _yaml_guard()},
    )
    assert result is None
    assert called is False
