# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""企业高级配置：代码默认铺底 + 模板列覆盖；不依赖 yaml。"""

from __future__ import annotations

from jiuwenswarm.enterprise.config.config_provider import _apply_enterprise_policy
from jiuwenswarm.enterprise.config.effective.apply_advanced_config import (
    DEFAULT_ADVANCED_CONFIG_SWITCHES,
    apply_enterprise_advanced_config_to_config,
    code_default_advanced_config_snapshot,
    enterprise_bare_runtime_config_snapshot,
    resolve_advanced_config_row,
    resolve_runtime_config_for_advanced_switches,
)
from jiuwenswarm.enterprise.config.effective.schemas import (
    EffectiveEnterpriseConfig,
    RoutingContext,
)


def _routing() -> RoutingContext:
    return RoutingContext(group_id="g1", bot_id="b1", user_id="u1")


def _enterprise(
    *,
    advanced_config: list[dict] | None,
) -> EffectiveEnterpriseConfig:
    return EffectiveEnterpriseConfig(
        routing=_routing(),
        resource_id="b1",
        ref_template_id="tpl-1",
        advanced_config=advanced_config,
    )


def test_resolve_advanced_config_row() -> None:
    row = {"template_id": "ac1", "enabled": True, "a4p_enabled": True}
    assert resolve_advanced_config_row(
        _enterprise(advanced_config=[row])
    ) == row
    assert resolve_advanced_config_row(None) is None
    assert resolve_advanced_config_row(_enterprise(advanced_config=None)) is None
    assert resolve_advanced_config_row(_enterprise(advanced_config=[])) is None
    assert (
        resolve_advanced_config_row(
            _enterprise(advanced_config=[{"template_id": "ac1", "enabled": False}])
        )
        is None
    )


def test_apply_sets_switches_from_template() -> None:
    base = {
        "a4p": {"enabled": False, "require_user_signature": False},
        "progressive_tool_enabled": True,
        "progressive_tool_optimize": False,
        "auto_memory_enabled": False,
        "kv_cache_affinity_config": {"enable_kv_cache_affinity": False},
    }
    enterprise = _enterprise(
        advanced_config=[
            {
                "template_id": "ac1",
                "enabled": True,
                "a4p_enabled": True,
                "progressive_tool_enabled": False,
                "progressive_tool_optimize": True,
                "auto_memory_enabled": True,
                "enable_kv_cache_affinity": True,
            }
        ]
    )

    merged, applied = apply_enterprise_advanced_config_to_config(base, enterprise)

    assert applied is True
    assert merged["a4p"]["enabled"] is True
    assert merged["a4p"]["require_user_signature"] is False
    assert merged["progressive_tool_enabled"] is False
    assert merged["progressive_tool_optimize"] is True
    assert merged["auto_memory_enabled"] is True
    assert merged["kv_cache_affinity_config"]["enable_kv_cache_affinity"] is True
    assert base["a4p"]["enabled"] is False


def test_apply_partial_template_fills_missing_with_code_defaults() -> None:
    base = {
        "progressive_tool_enabled": False,
        "a4p": {"enabled": True},
    }
    merged, applied = apply_enterprise_advanced_config_to_config(
        base,
        _enterprise(
            advanced_config=[
                {"template_id": "ac1", "enabled": True, "a4p_enabled": False}
            ]
        ),
    )
    assert applied is True
    assert merged["a4p"]["enabled"] is False
    assert (
        merged["progressive_tool_enabled"]
        is DEFAULT_ADVANCED_CONFIG_SWITCHES["progressive_tool_enabled"]
    )
    assert (
        merged["progressive_tool_optimize"]
        is DEFAULT_ADVANCED_CONFIG_SWITCHES["progressive_tool_optimize"]
    )
    assert (
        merged["auto_memory_enabled"]
        is DEFAULT_ADVANCED_CONFIG_SWITCHES["auto_memory_enabled"]
    )
    assert (
        merged["kv_cache_affinity_config"]["enable_kv_cache_affinity"]
        is DEFAULT_ADVANCED_CONFIG_SWITCHES["enable_kv_cache_affinity"]
    )


def test_apply_code_defaults_when_no_template() -> None:
    """未绑模板时忽略 base 里 yaml 遗留值，改用代码默认。"""
    base = {
        "a4p": {"enabled": True, "require_user_signature": True},
        "progressive_tool_enabled": False,
        "progressive_tool_optimize": True,
        "auto_memory_enabled": True,
        "kv_cache_affinity_config": {"enable_kv_cache_affinity": True, "extra": 1},
    }
    for enterprise in (None, _enterprise(advanced_config=None), _enterprise(advanced_config=[])):
        merged, applied = apply_enterprise_advanced_config_to_config(base, enterprise)
        assert applied is True
        assert merged is not base
        assert (
            merged["progressive_tool_enabled"]
            is DEFAULT_ADVANCED_CONFIG_SWITCHES["progressive_tool_enabled"]
        )
        assert (
            merged["progressive_tool_optimize"]
            is DEFAULT_ADVANCED_CONFIG_SWITCHES["progressive_tool_optimize"]
        )
        assert (
            merged["auto_memory_enabled"]
            is DEFAULT_ADVANCED_CONFIG_SWITCHES["auto_memory_enabled"]
        )
        assert merged["a4p"]["enabled"] is DEFAULT_ADVANCED_CONFIG_SWITCHES["a4p_enabled"]
        assert merged["a4p"]["require_user_signature"] is True
        assert (
            merged["kv_cache_affinity_config"]["enable_kv_cache_affinity"]
            is DEFAULT_ADVANCED_CONFIG_SWITCHES["enable_kv_cache_affinity"]
        )
        assert merged["kv_cache_affinity_config"]["extra"] == 1


def test_code_default_advanced_config_snapshot() -> None:
    snap = code_default_advanced_config_snapshot()
    assert (
        snap["progressive_tool_enabled"]
        is DEFAULT_ADVANCED_CONFIG_SWITCHES["progressive_tool_enabled"]
    )
    assert snap["a4p"]["enabled"] is DEFAULT_ADVANCED_CONFIG_SWITCHES["a4p_enabled"]
    assert (
        snap["kv_cache_affinity_config"]["enable_kv_cache_affinity"]
        is DEFAULT_ADVANCED_CONFIG_SWITCHES["enable_kv_cache_affinity"]
    )
    assert (
        snap["auto_memory_enabled"]
        is DEFAULT_ADVANCED_CONFIG_SWITCHES["auto_memory_enabled"]
    )


def test_resolve_runtime_prefers_explicit_config(monkeypatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.enterprise.edition.is_enterprise",
        lambda: True,
    )
    explicit = {"auto_memory_enabled": True, "a4p": {"enabled": True}}
    out = resolve_runtime_config_for_advanced_switches(config=explicit)
    assert out is explicit


def test_resolve_runtime_prefers_adapter_cache(monkeypatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.enterprise.edition.is_enterprise",
        lambda: True,
    )

    class _Adapter:
        _config_base_cache = {"auto_memory_enabled": True}

    out = resolve_runtime_config_for_advanced_switches(adapter=_Adapter())
    assert out["auto_memory_enabled"] is True


def test_resolve_runtime_enterprise_overlays_code_defaults_on_full_baseline(
    monkeypatch,
) -> None:
    """企业 bare：全量进程 config 作基底，五开关强制代码默认（覆盖 yaml）。"""
    monkeypatch.setattr(
        "jiuwenswarm.enterprise.edition.is_enterprise",
        lambda: True,
    )
    yaml_cfg = {
        "auto_memory_enabled": True,
        "a4p": {"enabled": True, "server_id": "keep-me"},
        "modes": {"agent": {"memory": {"enabled": True}}},
        "progressive_tool_enabled": False,
    }
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: yaml_cfg,
    )
    out = resolve_runtime_config_for_advanced_switches()
    assert out is not yaml_cfg
    assert (
        out["auto_memory_enabled"]
        is DEFAULT_ADVANCED_CONFIG_SWITCHES["auto_memory_enabled"]
    )
    assert out["a4p"]["enabled"] is DEFAULT_ADVANCED_CONFIG_SWITCHES["a4p_enabled"]
    assert out["a4p"]["server_id"] == "keep-me"
    assert out["modes"]["agent"]["memory"]["enabled"] is True
    assert (
        out["progressive_tool_enabled"]
        is DEFAULT_ADVANCED_CONFIG_SWITCHES["progressive_tool_enabled"]
    )
    bare = enterprise_bare_runtime_config_snapshot()
    assert bare["a4p"]["server_id"] == "keep-me"
    assert bare["a4p"]["enabled"] is DEFAULT_ADVANCED_CONFIG_SWITCHES["a4p_enabled"]


def test_resolve_runtime_personal_uses_yaml(monkeypatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.enterprise.edition.is_enterprise",
        lambda: False,
    )
    yaml_cfg = {"auto_memory_enabled": True, "from_yaml": True}
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: yaml_cfg,
    )
    out = resolve_runtime_config_for_advanced_switches()
    assert out is yaml_cfg


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
        advanced_config=[
            {"template_id": "ac1", "enabled": True, "a4p_enabled": True}
        ]
    )
    out = _apply_enterprise_policy(base, loaded)
    assert out["a4p"]["enabled"] is True
