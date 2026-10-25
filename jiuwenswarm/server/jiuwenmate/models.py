"""Colleague definitions, editable settings and the secret-free views returned to callers."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

_IDENTIFIER = re.compile(r"^[^\s\x00-\x1f\x7f]{1,512}$")
_MAX_NAME = 64
_MAX_DESCRIPTION = 2000
_MAX_PERSONA = 20000
_MAX_ITEMS = 64
_PERMISSION_PROFILES = frozenset({"default", "automatic", "full_access"})


class PersistentAgentError(ValueError):
    """A colleague operation was refused; ``code`` is stable for API mapping."""

    def __init__(self, message: str, *, code: str = "BAD_REQUEST") -> None:
        super().__init__(message)
        self.code = code


def require_identifier(name: str, value: Any) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise PersistentAgentError(f"invalid {name}")
    return value


def _text(name: str, value: Any, *, limit: int, required: bool) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise PersistentAgentError(f"invalid {name}")
    value = value.strip()
    if required and not value:
        raise PersistentAgentError(f"missing {name}")
    if len(value) > limit:
        raise PersistentAgentError(f"{name} is longer than {limit} characters")
    return value


def _items(name: str, values: Any) -> tuple[str, ...]:
    if values is None:
        return ()
    if not isinstance(values, (list, tuple)):
        raise PersistentAgentError(f"invalid {name}")
    items: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > 512:
            raise PersistentAgentError(f"invalid {name} item")
        if value.strip() not in items:
            items.append(value.strip())
    if len(items) > _MAX_ITEMS:
        raise PersistentAgentError(f"too many {name}")
    return tuple(items)


@dataclass(frozen=True, kw_only=True)
class CapabilitySelection:
    """Model and resources chosen for a colleague; assembly into tools belongs to the resource scope."""

    model: str | None = None
    skills: tuple[str, ...] = ()
    connectors: tuple[str, ...] = ()
    plugins: tuple[str, ...] = ()
    knowledge: tuple[str, ...] = ()
    starter_prompts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.model is not None:
            object.__setattr__(self, "model", _text("model", self.model, limit=512, required=True))
        for name in ("skills", "connectors", "plugins", "knowledge", "starter_prompts"):
            object.__setattr__(self, name, _items(name, getattr(self, name)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "skills": list(self.skills),
            "connectors": list(self.connectors),
            "plugins": list(self.plugins),
            "knowledge": list(self.knowledge),
            "starter_prompts": list(self.starter_prompts),
        }

    @classmethod
    def from_dict(cls, data: Any) -> CapabilitySelection:
        if data is None:
            return cls()
        if not isinstance(data, dict):
            raise PersistentAgentError("invalid capabilities")
        known = {"model", "skills", "connectors", "plugins", "knowledge", "starter_prompts"}
        unknown = set(data) - known
        if unknown:
            raise PersistentAgentError(f"unknown capability fields: {sorted(unknown)}")
        return cls(**data)


@dataclass(frozen=True, kw_only=True)
class ColleagueSettings:
    """Everything a user edits for one colleague; the persona is written into its Template."""

    name: str
    description: str = ""
    persona: str = ""
    tags: tuple[str, ...] = ()
    category: str = "custom"
    avatar_url: str = ""
    group_reply_mode: str = "adaptive"
    permission_profile: str = "default"
    capabilities: CapabilitySelection = field(default_factory=CapabilitySelection)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _text("name", self.name, limit=_MAX_NAME, required=True))
        object.__setattr__(
            self, "description", _text("description", self.description, limit=_MAX_DESCRIPTION, required=False)
        )
        object.__setattr__(self, "persona", _text("persona", self.persona, limit=_MAX_PERSONA, required=False))
        object.__setattr__(self, "tags", _items("tags", self.tags))
        object.__setattr__(self, "category", _text("category", self.category, limit=64, required=True))
        object.__setattr__(self, "avatar_url", _text("avatar_url", self.avatar_url, limit=2048, required=False))
        if self.group_reply_mode not in ("passive", "adaptive"):
            raise PersistentAgentError("invalid group_reply_mode")
        if not isinstance(self.permission_profile, str) or self.permission_profile not in _PERMISSION_PROFILES:
            raise PersistentAgentError("invalid permission_profile")
        if not isinstance(self.capabilities, CapabilitySelection):
            raise PersistentAgentError("invalid capabilities")

    def with_persona(self, persona: str) -> ColleagueSettings:
        return replace(self, persona=persona)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "persona": self.persona,
            "tags": list(self.tags),
            "category": self.category,
            "avatar_url": self.avatar_url,
            "group_reply_mode": self.group_reply_mode,
            "permission_profile": self.permission_profile,
            "capabilities": self.capabilities.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Any) -> ColleagueSettings:
        if not isinstance(data, dict):
            raise PersistentAgentError("invalid settings")
        known = {
            "name", "description", "persona", "tags", "category", "avatar_url", "capabilities",
            "group_reply_mode", "permission_profile",
        }
        unknown = set(data) - known
        if unknown:
            raise PersistentAgentError(f"unknown settings fields: {sorted(unknown)}")
        fields = dict(data)
        fields["capabilities"] = CapabilitySelection.from_dict(fields.get("capabilities"))
        return cls(**fields)


@dataclass(frozen=True, kw_only=True)
class ColleagueDefinition:
    """The persisted colleague; ``revision`` grows on every settings change."""

    namespace_id: str
    agent_id: str
    settings: ColleagueSettings
    source_template_id: str | None
    is_default: bool
    enabled: bool
    revision: int
    published_revision: int | None
    current_version: str | None
    created_at: datetime
    updated_at: datetime

    @property
    def has_unpublished_changes(self) -> bool:
        return self.published_revision != self.revision


@dataclass(frozen=True, kw_only=True)
class ConfigVersionView:
    """A published configuration version without the Spec, which may contain model credentials."""

    agent_id: str
    config_version: str
    fingerprint: str
    enabled_features: frozenset[str]
    published_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "config_version": self.config_version,
            "fingerprint": self.fingerprint,
            "enabled_features": sorted(self.enabled_features),
            "published_at": self.published_at.isoformat(),
        }


@dataclass(frozen=True, kw_only=True)
class ColleagueView:
    """What management APIs may return; never carries a Spec or model connection."""

    agent_id: str
    name: str
    description: str
    tags: tuple[str, ...]
    category: str
    avatar_url: str
    capabilities: CapabilitySelection
    source_template_id: str | None
    is_default: bool
    enabled: bool
    current_version: str | None
    has_unpublished_changes: bool
    created_at: datetime
    updated_at: datetime
    workspace_dir: str | None = None
    group_reply_mode: str = "adaptive"
    permission_profile: str = "default"

    @classmethod
    def from_definition(cls, definition: ColleagueDefinition) -> ColleagueView:
        settings = definition.settings
        return cls(
            agent_id=definition.agent_id,
            name=settings.name,
            description=settings.description,
            tags=settings.tags,
            category=settings.category,
            avatar_url=settings.avatar_url,
            group_reply_mode=settings.group_reply_mode,
            permission_profile=settings.permission_profile,
            capabilities=settings.capabilities,
            source_template_id=definition.source_template_id,
            is_default=definition.is_default,
            enabled=definition.enabled,
            current_version=definition.current_version,
            has_unpublished_changes=definition.has_unpublished_changes,
            created_at=definition.created_at,
            updated_at=definition.updated_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "description": self.description,
            "tags": list(self.tags),
            "category": self.category,
            "avatar_url": self.avatar_url,
            "capabilities": self.capabilities.to_dict(),
            "source_template_id": self.source_template_id,
            "is_default": self.is_default,
            "enabled": self.enabled,
            "current_version": self.current_version,
            "has_unpublished_changes": self.has_unpublished_changes,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "workspace_dir": self.workspace_dir,
            "group_reply_mode": self.group_reply_mode,
            "permission_profile": self.permission_profile,
        }
