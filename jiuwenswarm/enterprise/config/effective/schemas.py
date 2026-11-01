"""企业级配置路由上下文、加载结果与 ``template_ref`` 槽位定义。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class TemplateRefSlot(StrEnum):
    """``template_ref`` JSON 键名（与 agent_template.template_ref 槽位一致）。"""

    DEFAULT_MODEL = "default_model"
    VIDEO_MODEL = "video_model"
    AUDIO_MODEL = "audio_model"
    VISION_MODEL = "vision_model"
    IMAGE_GEN_MODEL = "image_gen_model"
    EMBEDDING_MODEL = "embedding_model"
    SKILL_PREBUILT = "skill_prebuilt"
    EXTENSION_CONFIG = "extension_config"
    MCP = "mcp"
    PERMISSIONS = "permissions"
    A2A_ACCESS_POLICY = "a2a_access_policy"


SLOT_ENTITY_TABLE: dict[TemplateRefSlot, str] = {
    TemplateRefSlot.DEFAULT_MODEL: "model_template",
    TemplateRefSlot.VIDEO_MODEL: "model_template",
    TemplateRefSlot.AUDIO_MODEL: "model_template",
    TemplateRefSlot.VISION_MODEL: "model_template",
    TemplateRefSlot.IMAGE_GEN_MODEL: "model_template",
    TemplateRefSlot.EMBEDDING_MODEL: "embedding_template",
    TemplateRefSlot.SKILL_PREBUILT: "skill_prebuilt_template",
    TemplateRefSlot.EXTENSION_CONFIG: "extension_config_template",
    TemplateRefSlot.MCP: "mcp_template",
    TemplateRefSlot.PERMISSIONS: "permissions_template",
    TemplateRefSlot.A2A_ACCESS_POLICY: "a2a_access_policy_template",
}

MODEL_SLOT_KEYS = frozenset({
    TemplateRefSlot.DEFAULT_MODEL,
    TemplateRefSlot.VIDEO_MODEL,
    TemplateRefSlot.AUDIO_MODEL,
    TemplateRefSlot.VISION_MODEL,
    TemplateRefSlot.IMAGE_GEN_MODEL,
})

DEFAULT_AGENT_LOAD_SLOTS = frozenset({
    *MODEL_SLOT_KEYS,
    TemplateRefSlot.EMBEDDING_MODEL,
    TemplateRefSlot.SKILL_PREBUILT,
    TemplateRefSlot.EXTENSION_CONFIG,
    TemplateRefSlot.MCP,
    TemplateRefSlot.PERMISSIONS,
    TemplateRefSlot.A2A_ACCESS_POLICY,
})


def normalize_template_ref(value: Any) -> dict[str, list[str]]:
    """将 ``template_ref`` 规范为 ``{slot: [ref_string, ...]}``；空值键省略，同槽位去重保序。"""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("template_ref must be a JSON object")
    out: dict[str, list[str]] = {}
    for key, raw in value.items():
        slot = str(key).strip()
        if not slot or raw is None:
            continue
        if not isinstance(raw, list):
            raise ValueError(f"template_ref[{slot!r}] must be a list")
        refs: list[str] = []
        seen: set[str] = set()
        for item in raw:
            if item is None:
                continue
            text = str(item).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            refs.append(text)
        if refs:
            out[slot] = refs
    return out


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
