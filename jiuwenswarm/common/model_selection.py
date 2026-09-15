"""Business DTOs for stable model and model-group selection."""

from __future__ import annotations

from typing import Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["model", "model_group"]
    id: str

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("id must not be empty")
        return value


class ResolvedModel(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    model_id: str
    source: Literal["defaults", "agentos"]
    model_name: str
    provider: str
    api_base: str = ""
    api_key: str = ""
    endpoint_profile: str | None = None
    fallback_tag: str | None = None
    model_description: str | None = None
    client_options: dict[str, Any] = Field(default_factory=dict)
    request_defaults: dict[str, Any] = Field(default_factory=dict)


class ResolvedRoute(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    route_id: str
    model: ResolvedModel
    enabled: bool = True
    request_overrides: dict[str, Any] = Field(default_factory=dict)
    tpm: int | None = None
    rpm: int | None = None
    timeout: float | None = None


class ResolvedModelGroup(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    model_group_id: str
    routes: list[ResolvedRoute]
    request_config: dict[str, Any] = Field(default_factory=dict)
    routing: dict[str, Any] = Field(default_factory=dict)


ResolvedSelection = Union[ResolvedModel, ResolvedModelGroup]

