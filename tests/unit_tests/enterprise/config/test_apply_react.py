# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.

"""enterprise react_template.data → config.react apply。"""

from __future__ import annotations

from jiuwenswarm.enterprise.config.config_provider import _apply_enterprise_policy
from jiuwenswarm.enterprise.config.effective.apply_react import (
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
