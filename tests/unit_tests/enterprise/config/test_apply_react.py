# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.

"""enterprise react_template.data → config.react apply（含 debug kvs 日志）。"""

from __future__ import annotations

import logging

from jiuwenswarm.enterprise.config.config_provider import _apply_enterprise_policy
from jiuwenswarm.enterprise.config.effective.apply_react import (
    _flatten_react_kvs,
    apply_enterprise_react_to_config,
)
from jiuwenswarm.enterprise.config.effective.schemas import (
    DEFAULT_AGENT_LOAD_SLOTS,
    EffectiveEnterpriseConfig,
    RoutingContext,
)


def _routing() -> RoutingContext:
    return RoutingContext(group_id="g1", bot_id="b1", user_id="u1")


def _enterprise(*, react) -> EffectiveEnterpriseConfig:
    return EffectiveEnterpriseConfig(routing=_routing(), resource_id="b1", react=react)


def test_default_load_slots_include_react() -> None:
    assert "react" in DEFAULT_AGENT_LOAD_SLOTS


def test_flatten_react_kvs_flattens_nested_dicts() -> None:
    data = {
        "max_iterations": 100,
        "context_engine_config": {"enabled": True, "window": {"size": 32}},
        "evolution": {},
        "subagents": ["a", "b"],
    }
    kvs = _flatten_react_kvs(data)
    assert "max_iterations=100" in kvs
    assert "context_engine_config.enabled=True" in kvs
    assert "context_engine_config.window.size=32" in kvs
    assert "evolution={}" in kvs
    assert "subagents=['a', 'b']" in kvs


def test_apply_replaces_yaml_react_with_enterprise_data() -> None:
    base = {
        "react": {
            "max_iterations": 1,
            "model_client_config": {"model_name": "yaml-model"},
        },
    }
    data = {
        "max_iterations": 100,
        "context_engine_config": {"enabled": True},
        "model_name": "mistakenly-bundled",
    }
    enterprise = _enterprise(react=[{"template_id": "r1", "data": data}])

    merged, applied = apply_enterprise_react_to_config(base, enterprise)

    assert applied is True
    # 整段覆盖
    assert merged["react"]["max_iterations"] == 100
    assert merged["react"]["context_engine_config"] == {"enabled": True}
    # 模型三键由模型槽位掌管：保留 apply_models 已写入的值，剥掉 data 误带的
    assert merged["react"]["model_client_config"] == {"model_name": "yaml-model"}
    assert "model_name" not in merged["react"]
    # 不污染入参
    assert base["react"]["max_iterations"] == 1
    assert "model_name" in data


def test_apply_keeps_yaml_when_no_react_entity() -> None:
    base = {"react": {"max_iterations": 1}}
    for enterprise in (
        None,
        _enterprise(react=None),
        _enterprise(react=[]),
        _enterprise(react=[{"template_id": "r1"}]),
        _enterprise(react=[{"template_id": "r1", "data": {}}]),
        _enterprise(react=["bad"]),
    ):
        merged, applied = apply_enterprise_react_to_config(base, enterprise)
        assert applied is False
        assert merged is base


def test_apply_debug_log_prints_all_react_kvs() -> None:
    """生效后应有一条 debug 日志展平打印 react 配置全部 key value。

    jiuwenswarm 祖先 logger 被 common.utils 配置成 propagate=False，
    caplog（挂在 root）收不到，所以直接往模块 logger 上挂 handler。
    """
    logger = logging.getLogger("jiuwenswarm.enterprise.config.effective.apply_react")
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append
    logger.addHandler(handler)
    old_level = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        base = {
            "react": {
                "model_client_config": {
                    "model_name": "yaml-model",
                    "api_key": "sk-secret",
                    "custom_headers": {"Authorization": "Bearer sk-secret"},
                }
            }
        }
        data = {
            "max_iterations": 100,
            "context_engine_config": {"enabled": True, "window": {"size": 32}},
            "model_name": "mistakenly-bundled",
        }
        enterprise = _enterprise(react=[{"template_id": "r1", "data": data}])
        _, applied = apply_enterprise_react_to_config(base, enterprise)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)

    assert applied is True
    kvs_records = [
        r for r in records if "applied react kvs" in r.getMessage()
    ]
    assert len(kvs_records) == 1
    text = kvs_records[0].getMessage()
    # data 的全部 key value（含嵌套展平）
    assert "max_iterations=100" in text
    assert "context_engine_config.enabled=True" in text
    assert "context_engine_config.window.size=32" in text
    # data 误带的模型键已剥掉，不出现
    assert "model_name='mistakenly-bundled'" not in text
    # 模型槽位保留的键仍在生效结果里，但凭据类叶子值掩码、明文不落日志
    assert "model_client_config.model_name='yaml-model'" in text
    assert "model_client_config.api_key=******" in text
    assert "model_client_config.custom_headers=******" in text
    assert "sk-secret" not in text


def test_apply_enterprise_policy_includes_react(monkeypatch) -> None:
    """_apply_enterprise_policy 应在 models/mcp/memory/permissions 之外写入 react。"""

    def _passthrough_models_mcp(base, loaded):
        return dict(base), False, False

    def _passthrough_memory(merged):
        return merged

    def _passthrough_permissions(merged, loaded):
        return merged, False

    def _noop_credentials(loaded):
        return None

    monkeypatch.setattr(
        "jiuwenswarm.enterprise.config.config_provider.apply_enterprise_models_and_mcp",
        _passthrough_models_mcp,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.memory.config.merge_memory_config_into_config",
        _passthrough_memory,
    )
    monkeypatch.setattr(
        "jiuwenswarm.enterprise.config.effective.apply_permissions.apply_enterprise_permissions_to_config",
        _passthrough_permissions,
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.mcp_dynamic_token_patch.apply_dispatched_credentials",
        _noop_credentials,
    )

    base = {"react": {"model_client_config": {"model_name": "yaml-model"}}}
    data = {"max_iterations": 100, "context_engine_config": {"enabled": True}}
    loaded = _enterprise(react=[{"template_id": "r1", "data": data}])

    out = _apply_enterprise_policy(base, loaded)
    assert out["react"]["max_iterations"] == 100
    assert out["react"]["context_engine_config"] == {"enabled": True}
    assert out["react"]["model_client_config"] == {"model_name": "yaml-model"}
