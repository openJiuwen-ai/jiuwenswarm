from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from urllib.parse import unquote, urlparse, urlsplit

from croniter import croniter
from pydantic import (
    AfterValidator,
    AliasChoices,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .safe_text import SafeTextMixin


# croniter：5 段标准；6 段末尾为秒；7 段为 分 时 日 月 周 秒 年
_CRON_FIELD_COUNTS = frozenset({5, 6, 7})


def is_valid_hook_schedule(value: str) -> bool:
    """用 croniter 校验 hook_config.schedule（含字段取值范围）。"""
    text = value.strip()
    if not text:
        return False
    if len(text.split()) not in _CRON_FIELD_COUNTS:
        return False
    return croniter.is_valid(text)


def normalize_hook_schedule(schedule: str | None, *, required: bool) -> str | None:
    """规范化 schedule；required 时不可为空，有值时须为合法 cron。"""
    text = (schedule or "").strip()
    if not text:
        if required:
            raise ValueError("hook_config.schedule is required when hook_type=schedule")
        return None
    if not is_valid_hook_schedule(text):
        raise ValueError(
            "hook_config.schedule must be a valid cron expression "
            "(5/6/7 fields via croniter, e.g. '0 */5 * * *' or '0 0 */5 * * *')"
        )
    return text


def _validate_http_url(value: str) -> str:
    """校验为合法 http(s) URL（须含主机）。"""
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("must be a valid http(s) URL")
    return value


def _optional_skill_source_url(value: Any) -> str | None:
    """空字符串视为未填；有值则按 http(s) URL 校验。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) > 2048:
        raise ValueError("package_url must be at most 2048 characters")
    return _validate_http_url(text)


SkillSourceUrl = Annotated[
    str,
    Field(min_length=1, max_length=2048),
    AfterValidator(_validate_http_url),
]
OptionalSkillSourceUrl = Annotated[
    str | None,
    BeforeValidator(_optional_skill_source_url),
]


def _validate_a2a_card_path(value: str) -> str:
    if not value.startswith("/") or value.startswith("//") or "\\" in value:
        raise ValueError("must be an absolute same-origin path")
    parsed = urlsplit(value)
    decoded = unquote(parsed.path)
    # G.CTL.03: if 内布尔条件不超过 3 个
    if parsed.scheme or parsed.netloc or parsed.query:
        raise ValueError("must not contain a scheme, host, query, or fragment")
    if parsed.fragment:
        raise ValueError("must not contain a scheme, host, query, or fragment")
    if decoded.startswith("//") or "\\" in decoded or ".." in decoded.split("/"):
        raise ValueError("must remain a same-origin path")
    return value


A2ASourceUrl = Annotated[
    str,
    Field(min_length=1, max_length=2048),
    AfterValidator(_validate_http_url),
]
A2ACardPath = Annotated[
    str,
    Field(min_length=1, max_length=512),
    AfterValidator(_validate_a2a_card_path),
]
TemplateId = Annotated[str, Field(min_length=1, max_length=100)]


class HookConfig(BaseModel):
    """扩展模板 hook_config 结构（与设计文档一致）。"""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    handler: str = Field(..., min_length=1)
    params: dict[str, Any] | None = None
    schedule: str | None = None
    data: dict[str, Any] | None = None


class ModelTemplateUpdateRequest(SafeTextMixin):
    template_name: str | None = Field(default=None, max_length=128)
    description: str | None = Field(default=None, max_length=512)
    model_type: list[str] | None = None
    model_tags: list[str] | None = None
    api_base: str | None = Field(default=None, max_length=512)
    api_key: str | None = None
    model_id: str | None = Field(default=None, max_length=128)
    model_provider: str | None = Field(default=None, max_length=64)
    parameters: dict[str, Any] | None = None
    timeout: int | None = Field(default=None, ge=1)
    retry_count: int | None = Field(default=None, ge=0)
    enable_streaming: bool | None = None
    enable_function_calling: bool | None = None
    verify_ssl: bool | None = None
    enabled: bool | None = None
    data: dict[str, Any] | None = None


class ModelTemplateCreateRequest(ModelTemplateUpdateRequest):
    """同步创建：含 ``template_id`` + 业务字段。"""

    template_id: str = Field(..., min_length=1, max_length=100)
    template_name: str = Field(..., min_length=1, max_length=128)
    api_base: str = Field(..., min_length=1, max_length=512)
    api_key: str = Field(..., min_length=1)
    model_id: str = Field(..., min_length=1, max_length=128)
    model_provider: str = Field(..., min_length=1, max_length=64)


class EmbeddingTemplateUpdateRequest(SafeTextMixin):
    template_name: str | None = Field(
        default=None,
        max_length=128,
        validation_alias=AliasChoices("template_name", "name"),
    )
    description: str | None = Field(default=None, max_length=512)
    embed_tags: list[str] | None = None
    api_base: str | None = Field(default=None, max_length=512)
    api_key: str | None = None
    model_id: str | None = Field(default=None, max_length=128)
    model_provider: str | None = Field(default=None, max_length=64)
    parameters: dict[str, Any] | None = None
    client_config: dict[str, Any] | None = None
    enabled: bool | None = None
    data: dict[str, Any] | None = None


class EmbeddingTemplateCreateRequest(EmbeddingTemplateUpdateRequest):
    template_id: str = Field(..., min_length=1, max_length=100)
    template_name: str = Field(..., min_length=1, max_length=128)
    api_base: str = Field(..., min_length=1, max_length=512)
    api_key: str = Field(..., min_length=1)
    model_id: str = Field(..., min_length=1, max_length=128)
    model_provider: str = Field(..., min_length=1, max_length=64)


class ExtensionConfigTemplateUpdateRequest(SafeTextMixin):
    template_name: str | None = Field(default=None, max_length=128)
    description: str | None = Field(default=None, max_length=512)
    component: str | None = Field(default=None, max_length=32)
    hook_type: str | None = Field(default=None, max_length=32)
    hook_config: HookConfig | None = None
    custom_config: dict[str, Any] | None = None
    enabled: bool | None = None
    data: dict[str, Any] | None = None


class ExtensionConfigTemplateCreateRequest(ExtensionConfigTemplateUpdateRequest):
    template_id: str = Field(..., min_length=1, max_length=100)
    template_name: str = Field(..., min_length=1, max_length=128)
    component: str = Field(..., min_length=1, max_length=32)
    hook_type: str = Field(..., min_length=1, max_length=32)


class SkillPrebuiltTemplateUpdateRequest(SafeTextMixin):
    template_name: str | None = Field(default=None, max_length=128)
    description: str | None = Field(default=None, max_length=512)
    skill_id: str | None = Field(default=None, max_length=512)
    package_url: OptionalSkillSourceUrl = None
    source_id: str | None = Field(default=None, max_length=64)
    version_id: str | None = Field(default=None, max_length=128)
    enabled: bool | None = None
    data: dict[str, Any] | None = None


class SkillPrebuiltTemplateCreateRequest(SkillPrebuiltTemplateUpdateRequest):
    template_id: str = Field(..., min_length=1, max_length=100)
    template_name: str = Field(..., min_length=1, max_length=128)
    skill_id: str = Field(..., min_length=1, max_length=512)


_VALID_MCP_TRANSPORTS = frozenset({
    "stdio",
    "sse",
    "http",
    "streamable-http",
    "streamable_http",
})


def validate_mcp_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """校验 MCP 模板 ``mcp_entry``；丢弃条目级 ``enabled``（开关只认模板行）。"""
    if not isinstance(entry, dict):
        raise ValueError("mcp_entry must be a JSON object")
    normalized = dict(entry)
    normalized.pop("enabled", None)
    name = str(normalized.get("name", "")).strip()
    if not name:
        raise ValueError("mcp_entry.name is required")
    transport = str(normalized.get("transport", "")).strip().lower()
    if transport not in _VALID_MCP_TRANSPORTS:
        raise ValueError(
            "mcp_entry.transport must be one of: "
            + ", ".join(sorted(_VALID_MCP_TRANSPORTS))
        )
    if transport == "stdio":
        command = str(normalized.get("command", "")).strip()
        if not command:
            raise ValueError("mcp_entry.command is required for stdio transport")
    else:
        url = str(normalized.get("url", "")).strip()
        if not url:
            raise ValueError("mcp_entry.url is required for remote MCP transport")
    normalized["name"] = name
    normalized["transport"] = transport
    return normalized


class McpTemplateUpdateRequest(SafeTextMixin):
    template_name: str | None = Field(default=None, max_length=128)
    description: str | None = Field(default=None, max_length=512)
    mcp_entry: dict[str, Any] | None = None
    enabled: bool | None = None
    data: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _validate_entry(self) -> McpTemplateUpdateRequest:
        if self.mcp_entry is not None:
            validate_mcp_entry(self.mcp_entry)
        return self


class McpTemplateCreateRequest(McpTemplateUpdateRequest):
    template_id: str = Field(..., min_length=1, max_length=100)
    template_name: str = Field(..., min_length=1, max_length=128)
    mcp_entry: dict[str, Any]


class PermissionsTemplateUpdateRequest(SafeTextMixin):
    template_name: str | None = Field(default=None, max_length=128)
    description: str | None = Field(default=None, max_length=512)
    enabled: bool | None = None
    body: dict[str, Any] | None = None
    data: dict[str, Any] | None = None


class PermissionsTemplateCreateRequest(PermissionsTemplateUpdateRequest):
    template_id: str = Field(..., min_length=1, max_length=100)
    template_name: str = Field(..., min_length=1, max_length=128)
    body: dict[str, Any] = Field(
        ...,
        description=(
            "完整 permissions 段，结构与 config.yaml::permissions 一致"
        ),
    )


class AgentTemplateUpdateRequest(SafeTextMixin):
    template_name: str | None = Field(default=None, max_length=128)
    description: str | None = Field(default=None, max_length=512)
    agent_tags: list[str] | None = None
    template_ref: dict[str, list[str]] | None = None
    enabled: bool | None = None
    data: dict[str, Any] | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_template_ref_field(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("template_ref") is not None:
            from ..infrastructure.utils import normalize_template_ref

            data = dict(data)
            data["template_ref"] = normalize_template_ref(data["template_ref"])
        return data


class AgentTemplateCreateRequest(AgentTemplateUpdateRequest):
    template_id: str = Field(..., min_length=1, max_length=100)
    template_name: str = Field(..., min_length=1, max_length=128)
    template_ref: dict[str, list[str]] = Field(default_factory=dict)


class A2ACredentialOperation(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    operation: Literal["keep", "replace", "clear"]
    value: str | None = Field(default=None, max_length=4096)

    @model_validator(mode="after")
    def _validate_operation(self) -> A2ACredentialOperation:
        if self.operation == "replace":
            if not self.value:
                raise ValueError("credential.value is required for replace")
            if self.value.startswith("ENC:v1:"):
                raise ValueError("encrypted credential envelopes are not supported")
        elif self.value is not None:
            raise ValueError("credential.value is only valid for replace")
        return self


class A2AOutboundTemplateUpdateRequest(SafeTextMixin):
    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")

    template_name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=512)
    a2a_tags: list[str] | None = None
    source_url: A2ASourceUrl | None = None
    card_path: A2ACardPath | None = None
    agent_card: dict[str, Any] | None = None
    card_fingerprint: str | None = Field(default=None, min_length=1, max_length=128)
    card_revision: int | None = Field(default=None, ge=1)
    selected_interface: dict[str, Any] | None = None
    connect_timeout_seconds: float | None = Field(default=None, gt=0)
    sync_wait_seconds: float | None = Field(default=None, gt=0)
    enabled: bool | None = None
    credential: A2ACredentialOperation | None = None
    data: dict[str, Any] | None = None
    updated_at: datetime | None = None

    @model_validator(mode="after")
    def _validate_card_and_interface(self) -> A2AOutboundTemplateUpdateRequest:
        if self.agent_card is not None and not str(self.agent_card.get("name") or "").strip():
            raise ValueError("agent_card.name is required")
        if self.selected_interface is not None:
            protocol_binding = str(
                self.selected_interface.get("protocol_binding") or ""
            ).strip()
            protocol_version = str(
                self.selected_interface.get("protocol_version") or ""
            ).strip()
            if not protocol_binding:
                raise ValueError("selected_interface.protocol_binding is required")
            if not protocol_version:
                raise ValueError("selected_interface.protocol_version is required")
            interface_url = str(self.selected_interface.get("url") or "").strip()
            if not interface_url:
                raise ValueError("selected_interface.url is required")
            _validate_http_url(interface_url)
        return self


class A2AOutboundTemplateCreateRequest(A2AOutboundTemplateUpdateRequest):
    template_id: TemplateId
    template_name: str = Field(..., min_length=1, max_length=128)
    source_url: A2ASourceUrl
    card_path: A2ACardPath
    agent_card: dict[str, Any]
    card_fingerprint: str = Field(..., min_length=1, max_length=128)
    card_revision: int = Field(..., ge=1)
    selected_interface: dict[str, Any]
    connect_timeout_seconds: float = Field(..., gt=0)
    sync_wait_seconds: float = Field(..., gt=0)
    enabled: bool
    credential: A2ACredentialOperation
    updated_at: datetime

    @model_validator(mode="after")
    def _require_bootstrap_credential(self) -> A2AOutboundTemplateCreateRequest:
        if self.credential.operation == "keep":
            raise ValueError("credential keep is not valid for A2A Agent upsert")
        return self


class A2AAccessPolicyTemplateUpdateRequest(SafeTextMixin):
    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")

    policy_name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=512)
    mode: Literal["allowlist", "denylist"] | None = None
    member_template_ids: list[TemplateId] | None = None
    enabled: bool | None = None
    revision: int | None = Field(default=None, ge=1)
    data: dict[str, Any] | None = None
    updated_at: datetime | None = None

    @field_validator("member_template_ids")
    @classmethod
    def _deduplicate_members(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else list(dict.fromkeys(value))


class A2AAccessPolicyTemplateCreateRequest(A2AAccessPolicyTemplateUpdateRequest):
    policy_id: TemplateId
    policy_name: str = Field(..., min_length=1, max_length=128)
    mode: Literal["allowlist", "denylist"]
    member_template_ids: list[TemplateId] = Field(default_factory=list)
    enabled: bool
    revision: int = Field(..., ge=1)
    updated_at: datetime
