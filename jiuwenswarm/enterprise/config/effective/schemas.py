"""企业级配置路由上下文、加载结果与 ``template_ref`` 槽位定义。

槽位枚举、槽位 → 实体表映射与 ``normalize_template_ref`` 的唯一实现在
``e2a.template_slots``；本模块 re-export 保持既有导入路径，
只保留依赖本仓加载流程的数据快照。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from e2a.template_slots import (
    DEFAULT_AGENT_LOAD_SLOTS,
    MODEL_SLOT_KEYS,
    SLOT_ENTITY_TABLE,
    TemplateRefSlot,
    normalize_template_ref,
)

# react / execution_guard 槽位以字符串接线：e2a.template_slots（协议包，固定 commit）
# 尚未登记这两个槽位，这里在仓内派生默认加载槽位集合，待协议包发版后收敛。
# 元素为 TemplateRefSlot 枚举与字符串的混合集合，loader 侧统一取 .value 收敛
# （TemplateRefSlot 是 StrEnum，字符串比较语义一致）。
REACT_SLOT = "react"
REACT_ENTITY_TABLE = "react_template"
EXECUTION_GUARD_SLOT = "execution_guard"
EXECUTION_GUARD_ENTITY_TABLE = "execution_guard_template"
DEFAULT_AGENT_LOAD_SLOTS = frozenset({
    *DEFAULT_AGENT_LOAD_SLOTS,
    REACT_SLOT,
    EXECUTION_GUARD_SLOT,
})


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
    # react 运行策略（react_template.data 按键深度合并 config.react：
    # data 配置键覆盖，未配置键保留本地值）。
    react: list[dict[str, Any]] | None = None
    # 执行护栏（execution_guard_template.body 整段替换 config.execution_guard）。
    execution_guard: list[dict[str, Any]] | None = None
    a2a_access_policy: list[dict[str, Any]] | None = None
    # Gateway advanced_config 表行（按 agent_template_id）；含 a4p_enabled 等。
    advanced_config: dict[str, Any] | None = None
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
            "react": self.react,
            "execution_guard": self.execution_guard,
            "a2a_access_policy": self.a2a_access_policy,
            "advanced_config": self.advanced_config,
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
