# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""enterprise permissions_template.body → config.permissions apply."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace


def _install_gateway_protocol_stub() -> None:
    """Minimal stub so enterprise_config package import works without the wire package."""
    if "gateway_protocol.e2a.agent_models" in sys.modules:
        return
    root = ModuleType("gateway_protocol")
    e2a = ModuleType("gateway_protocol.e2a")
    agent_models = ModuleType("gateway_protocol.e2a.agent_models")
    for name in (
        "AgentRequest",
        "AgentResponse",
        "AgentResponseChunk",
        "PermissionContext",
    ):
        setattr(agent_models, name, type(name, (), {}))
    sys.modules["gateway_protocol"] = root
    sys.modules["gateway_protocol.e2a"] = e2a
    sys.modules["gateway_protocol.e2a.agent_models"] = agent_models
    root.e2a = e2a
    e2a.agent_models = agent_models


_install_gateway_protocol_stub()

from jiuwenswarm.enterprise.config.config_provider import _apply_enterprise_policy
from jiuwenswarm.enterprise.config.effective.apply_permissions import (
    apply_enterprise_permissions_to_config,
)
from jiuwenswarm.enterprise.config.effective.permissions import (
    resolve_permissions_body_from_enterprise,
)
from jiuwenswarm.enterprise.config.effective.schemas import (
    EffectiveEnterpriseConfig,
    RoutingContext,
)


def _routing() -> RoutingContext:
    return RoutingContext(group_id="g1", bot_id="b1", user_id="u1")


def _enterprise(*, permissions: list | None) -> EffectiveEnterpriseConfig:
    return EffectiveEnterpriseConfig(
        routing=_routing(),
        resource_id="b1",
        permissions=permissions,
    )


def test_resolve_permissions_body_takes_first_enabled_template() -> None:
    body = {
        "enabled": True,
        "defaults": {"*": "ask"},
        "tools": {},
    }
    enterprise = _enterprise(
        permissions=[
            {"template_id": "p0", "enabled": False, "body": {"enabled": False}},
            {"template_id": "p1", "enabled": True, "body": body},
        ]
    )
    resolved = resolve_permissions_body_from_enterprise(enterprise)
    assert resolved == body
    assert resolved is not body


def test_resolve_permissions_body_none_when_missing_or_disabled() -> None:
    assert resolve_permissions_body_from_enterprise(None) is None
    assert resolve_permissions_body_from_enterprise(_enterprise(permissions=None)) is None
    assert (
        resolve_permissions_body_from_enterprise(
            _enterprise(
                permissions=[
                    {"template_id": "p0", "enabled": False, "body": {"enabled": True}},
                ]
            )
        )
        is None
    )
    assert resolve_permissions_body_from_enterprise(SimpleNamespace(permissions="bad")) is None


def test_apply_replaces_yaml_permissions_with_enterprise_body() -> None:
    base = {
        "permissions": {
            "enabled": False,
            "defaults": {"*": "allow"},
            "tools": {"bash": "allow", "write_file": "allow"},
        },
        "models": {"default": {}},
    }
    body = {
        "enabled": True,
        "schema": "tiered_policy",
        "permission_mode": "normal",
        "defaults": {"*": "ask"},
        "tools": {},
    }
    enterprise = _enterprise(
        permissions=[{"template_id": "p1", "enabled": True, "body": body}]
    )

    merged, applied = apply_enterprise_permissions_to_config(base, enterprise)

    assert applied is True
    assert merged["permissions"] == body
    assert merged["permissions"]["enabled"] is True
    assert merged["permissions"]["defaults"] == {"*": "ask"}
    # yaml tools allow 不得残留
    assert "bash" not in merged["permissions"].get("tools", {})
    # 不污染入参
    assert base["permissions"]["enabled"] is False


def test_apply_keeps_yaml_when_no_permissions_body() -> None:
    base = {"permissions": {"enabled": False, "defaults": {"*": "allow"}}}
    merged, applied = apply_enterprise_permissions_to_config(base, None)
    assert applied is False
    assert merged is base

    merged2, applied2 = apply_enterprise_permissions_to_config(
        base, _enterprise(permissions=[])
    )
    assert applied2 is False
    assert merged2 is base


def test_apply_enterprise_policy_includes_permissions(monkeypatch) -> None:
    """_apply_enterprise_policy 应在 models/mcp/memory 之外写入 permissions。"""

    def _passthrough_models_mcp(base, loaded):
        return dict(base), False, False

    def _passthrough_memory(merged):
        return merged

    monkeypatch.setattr(
        "jiuwenswarm.enterprise.config.config_provider.apply_enterprise_models_and_mcp",
        _passthrough_models_mcp,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.memory.config.merge_memory_config_into_config",
        _passthrough_memory,
    )

    base = {
        "permissions": {
            "enabled": False,
            "defaults": {"*": "allow"},
            "tools": {"bash": "allow"},
        }
    }
    body = {"enabled": True, "defaults": {"*": "ask"}, "tools": {}}
    loaded = _enterprise(
        permissions=[{"template_id": "p1", "enabled": True, "body": body}]
    )

    out = _apply_enterprise_policy(base, loaded)
    assert out["permissions"]["enabled"] is True
    assert out["permissions"]["defaults"] == {"*": "ask"}
    assert "bash" not in out["permissions"].get("tools", {})
