# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""advanced_config.a4p_enabled → config.a4p.enabled apply."""

from __future__ import annotations

from jiuwenswarm.enterprise.config.config_provider import _apply_enterprise_policy
from jiuwenswarm.enterprise.config.effective.apply_advanced_config import (
    apply_enterprise_advanced_config_to_config,
    resolve_advanced_config_row,
)
from jiuwenswarm.enterprise.config.effective.schemas import (
    EffectiveEnterpriseConfig,
    RoutingContext,
)


def _routing() -> RoutingContext:
    return RoutingContext(group_id="g1", bot_id="b1", user_id="u1")


def _enterprise(*, advanced_config: dict | None) -> EffectiveEnterpriseConfig:
    return EffectiveEnterpriseConfig(
        routing=_routing(),
        resource_id="b1",
        ref_template_id="tpl-1",
        advanced_config=advanced_config,
    )


def test_resolve_advanced_config_row() -> None:
    row = {"agent_template_id": "tpl-1", "a4p_enabled": True}
    assert resolve_advanced_config_row(_enterprise(advanced_config=row)) == row
    assert resolve_advanced_config_row(None) is None
    assert resolve_advanced_config_row(_enterprise(advanced_config=None)) is None


def test_apply_sets_a4p_enabled_true() -> None:
    base = {
        "a4p": {"enabled": False, "require_user_signature": False},
        "progressive_tool_enabled": True,
    }
    enterprise = _enterprise(
        advanced_config={
            "agent_template_id": "tpl-1",
            "a4p_enabled": True,
            "progressive_tool_enabled": False,
            "progressive_tool_optimize": True,
            "auto_memory_enabled": True,
            "enable_kv_cache_affinity": True,
        }
    )

    merged, applied = apply_enterprise_advanced_config_to_config(base, enterprise)

    assert applied is True
    assert merged["a4p"]["enabled"] is True
    assert merged["a4p"]["require_user_signature"] is False
    assert merged["progressive_tool_enabled"] is False
    assert merged["progressive_tool_optimize"] is True
    assert merged["auto_memory_enabled"] is True
    assert merged["kv_cache_affinity_config"]["enable_kv_cache_affinity"] is True
    # 不污染入参
    assert base["a4p"]["enabled"] is False


def test_apply_sets_a4p_enabled_false() -> None:
    base = {"a4p": {"enabled": True}}
    merged, applied = apply_enterprise_advanced_config_to_config(
        base,
        _enterprise(
            advanced_config={"agent_template_id": "tpl-1", "a4p_enabled": False}
        ),
    )
    assert applied is True
    assert merged["a4p"]["enabled"] is False


def test_apply_keeps_yaml_when_no_advanced_config() -> None:
    base = {"a4p": {"enabled": False}}
    merged, applied = apply_enterprise_advanced_config_to_config(base, None)
    assert applied is False
    assert merged is base

    merged2, applied2 = apply_enterprise_advanced_config_to_config(
        base, _enterprise(advanced_config=None)
    )
    assert applied2 is False
    assert merged2 is base


def test_apply_enterprise_policy_includes_a4p(monkeypatch) -> None:
    def _passthrough_models_mcp(base, loaded):
        return dict(base), False, False

    def _passthrough_memory(merged):
        return merged

    def _passthrough_permissions(merged, loaded):
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

    base = {"a4p": {"enabled": False}}
    loaded = _enterprise(
        advanced_config={"agent_template_id": "tpl-1", "a4p_enabled": True}
    )
    out = _apply_enterprise_policy(base, loaded)
    assert out["a4p"]["enabled"] is True
