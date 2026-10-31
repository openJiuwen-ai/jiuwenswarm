# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Mount-scoped dependencies shared by native runtime plugins."""

from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Mapping


def freeze(value: Any) -> Any:
    """Freeze configuration containers without copying live service objects."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class PluginRuntimeServices:
    """Only declared services are present; business validation belongs to the ZIP."""

    plugin_id: str
    session_id: str
    member_card_id: str
    profile: str
    services: Mapping[str, Any] = field(repr=False)
    host_config: Mapping[str, Any] = field(default_factory=dict, repr=False)
    package_config: Mapping[str, Any] = field(default_factory=dict, repr=False)
    options: Mapping[str, Any] = field(default_factory=dict, repr=False)
    resources: list[Any] = field(default_factory=list, repr=False, compare=False)
    contract_version: int = 1
    role_scope: str = "main"

    def __post_init__(self) -> None:
        for name in ("services", "host_config", "package_config", "options"):
            object.__setattr__(self, name, freeze(getattr(self, name)))

    def require(self, name: str) -> Any:
        if name not in self.services or self.services[name] is None:
            raise RuntimeError(f"Plugin {self.plugin_id} requires unavailable service: {name}")
        return self.services[name]

    def with_options(self, options: Mapping[str, Any]) -> "PluginRuntimeServices":
        """Keep the dependency snapshot while installing package-validated options."""
        return replace(self, options=options)

    def register_resource(self, resource: Any) -> Any:
        """Track constructed Rails before init for partial-binding compensation."""
        if any(item is resource for item in self.resources):
            raise ValueError("Plugin resource is already tracked")
        self.resources.append(resource)
        return resource
