# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""a2ui_config → config.a2ui apply（含 fail closed）。"""

from __future__ import annotations

from jiuwenswarm.enterprise.config.config_provider import _apply_enterprise_policy
from jiuwenswarm.enterprise.config.effective.apply_a2ui_config import (
    apply_enterprise_a2ui_config_to_config,
    resolve_a2ui_config_row,
)
from jiuwenswarm.enterprise.config.effective.schemas import (
    EffectiveEnterpriseConfig,
    RoutingContext,
)


def _routing() -> RoutingContext:
    return RoutingContext(group_id="g1", bot_id="b1", user_id="u1")


def _enterprise(*, a2ui_config: dict | None) -> EffectiveEnterpriseConfig:
    return EffectiveEnterpriseConfig(
        routing=_routing(),
        resource_id="b1",
        ref_template_id="tpl-1",
        a2ui_config=a2ui_config,
    )


def test_resolve_a2ui_config_row() -> None:
    row = {"agent_template_id": "tpl-1", "enabled": True}
    assert resolve_a2ui_config_row(_enterprise(a2ui_config=row)) == row
    assert resolve_a2ui_config_row(None) is None
    assert resolve_a2ui_config_row(_enterprise(a2ui_config=None)) is None


def test_apply_row_overrides_yaml_enabled_true() -> None:
    base = {
        "a2ui": {
            "enabled": False,
            "protocol_version": "0.8",
            "stream_validation_enabled": True,
            "dev_smoke_tools_enabled": False,
        }
    }
    enterprise = _enterprise(
        a2ui_config={
            "agent_template_id": "tpl-1",
            "enabled": True,
            "protocol_version": "0.8",
            "stream_validation_enabled": False,
            "dev_smoke_tools_enabled": True,
            "data": {"extra_flag": True, "enabled": "should-not-win"},
        }
    )

    merged, applied = apply_enterprise_a2ui_config_to_config(base, enterprise)

    assert applied is True
    assert merged["a2ui"]["enabled"] is True
    assert merged["a2ui"]["protocol_version"] == "0.8"
    assert merged["a2ui"]["stream_validation_enabled"] is False
    assert merged["a2ui"]["dev_smoke_tools_enabled"] is True
    assert merged["a2ui"]["extra_flag"] is True
    # data 不得覆盖独立列（enabled 仍以列为准）
    assert merged["a2ui"]["enabled"] is True
    assert base["a2ui"]["enabled"] is False


def test_apply_fail_closed_when_no_row_overrides_yaml_true() -> None:
    base = {
        "a2ui": {
            "enabled": True,
            "protocol_version": "0.8",
            "stream_validation_enabled": True,
            "dev_smoke_tools_enabled": False,
        }
    }

    merged, applied = apply_enterprise_a2ui_config_to_config(base, None)
    assert applied is True
    assert merged["a2ui"]["enabled"] is False
    assert base["a2ui"]["enabled"] is True

    merged2, applied2 = apply_enterprise_a2ui_config_to_config(
        base, _enterprise(a2ui_config=None)
    )
    assert applied2 is True
    assert merged2["a2ui"]["enabled"] is False


def test_apply_enterprise_policy_includes_a2ui(monkeypatch) -> None:
    def _passthrough_models_mcp(base, loaded):
        return dict(base), False, False

    def _passthrough_memory(merged):
        return merged

    def _passthrough_permissions(merged, loaded):
        return merged, False

    def _passthrough_advanced(merged, loaded):
        return merged, False

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
        "jiuwenswarm.enterprise.config.effective.apply_advanced_config.apply_enterprise_advanced_config_to_config",
        _passthrough_advanced,
    )

    base = {"a2ui": {"enabled": False}}
    loaded = _enterprise(
        a2ui_config={
            "agent_template_id": "tpl-1",
            "enabled": True,
            "protocol_version": "0.8",
        }
    )
    out = _apply_enterprise_policy(base, loaded)
    assert out["a2ui"]["enabled"] is True
