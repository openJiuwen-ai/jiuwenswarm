"""企业级配置路由上下文、加载结果与 ``template_ref`` 槽位定义。

槽位枚举、槽位 → 实体表映射与 ``normalize_template_ref`` 的唯一实现在
``openjiuwen_protocol.template_slots``；本模块 re-export 保持既有导入路径，
只保留依赖本仓加载流程的数据快照。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from openjiuwen_protocol.template_slots import (
    DEFAULT_AGENT_LOAD_SLOTS,
    MODEL_SLOT_KEYS,
    SLOT_ENTITY_TABLE,
    TemplateRefSlot,
    normalize_template_ref,
)


@dataclass(frozen=True)
class RoutingContext:
    """企业配置路由三元组；不含 ``gateway_id``（Agent 业务不消费）。"""

    group_id: str
    bot_id: str
    user_id: str

    def as_dict(self) -> dict[str, str]:
        return {
            "group_id": self.group_id,
            "bot_id": self.bot_id,
            "user_id": self.user_id,
        }


@dataclass
class EffectiveEnterpriseConfig:
    """单次路由上下文下解析完成的企业级配置快照。"""

    routing: RoutingContext
    resource_id: str | None = None
    instance_agent_resource: dict[str, Any] | None = None
    ref_template_id: str | None = None
    agent_template: dict[str, Any] | None = None
    template_ref: dict[str, list[str]] = field(default_factory=dict)
    models: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    embedding: list[dict[str, Any]] | None = None
    skill_prebuilt: list[dict[str, Any]] | None = None
    extension_config: list[dict[str, Any]] | None = None
    mcp: list[dict[str, Any]] | None = None
    permissions: list[dict[str, Any]] | None = None
    a2a_access_policy: list[dict[str, Any]] | None = None
    service_id: str | None = None
    send_file_allowed: bool = True
    debug: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "routing": self.routing.as_dict(),
            "resource_id": self.resource_id,
            "instance_agent_resource": self.instance_agent_resource,
            "ref_template_id": self.ref_template_id,
            "agent_template": self.agent_template,
            "template_ref": dict(self.template_ref),
            "models": dict(self.models),
            "embedding": self.embedding,
            "skill_prebuilt": self.skill_prebuilt,
            "extension_config": self.extension_config,
            "mcp": self.mcp,
            "permissions": self.permissions,
            "a2a_access_policy": self.a2a_access_policy,
            "service_id": self.service_id,
            "send_file_allowed": self.send_file_allowed,
            "debug": dict(self.debug),
        }


__all__ = (
    "DEFAULT_AGENT_LOAD_SLOTS",
    "EffectiveEnterpriseConfig",
    "MODEL_SLOT_KEYS",
    "RoutingContext",
    "SLOT_ENTITY_TABLE",
    "TemplateRefSlot",
    "normalize_template_ref",
)
