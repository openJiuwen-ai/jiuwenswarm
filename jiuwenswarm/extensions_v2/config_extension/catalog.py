# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Explicit public projection of existing, non-sensitive host settings."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ConfigField:
    """One public key and its authoritative native storage/panel adapter."""

    key: str
    native_key: str
    default: bool | str
    label: str
    description: str
    options: tuple[str, ...] = ()

    @property
    def path(self) -> tuple[str, ...]:
        return tuple(self.key.split("."))

    def validate(self, value: object) -> bool:
        if isinstance(self.default, bool):
            return type(value) is bool
        return isinstance(value, str) and value in self.options

    def candidate(self, value: object) -> bool | str:
        """Normalize native panel booleans without inventing a second writer."""
        if isinstance(self.default, bool):
            if type(value) is bool:
                return value
            if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
                return value.strip().lower() == "true"
        elif isinstance(value, str):
            return value
        raise ValueError("invalid configuration value type")

    def read(self, config: Mapping) -> bool | str:
        value = config
        for part in self.path:
            if not isinstance(value, Mapping) or part not in value:
                return self.default
            value = value[part]
        try:
            normalized = self.candidate(value)
        except ValueError:
            return self.default
        return normalized if self.validate(normalized) else self.default


FIELDS = (
    ConfigField("preferred_language", "preferred_language", "zh", "Preferred language",
                "Interface language: zh for Chinese, en for English.", ("zh", "en")),
    ConfigField("react.context_engine_config.enabled", "context_engine_enabled", False,
                "Context engine", "Enable the existing context engine for supported agents."),
    ConfigField("rsi.enabled", "rsi_enabled", False, "RSI", "Enable the native RSI entry."),
    ConfigField("rea.enabled", "rea_enabled", False, "REA", "Enable the native REA entry."),
)
BY_KEY = {field.key: field for field in FIELDS}
PROVIDER = {"plugin": "engine", "tier": "core"}


def require_scope(scope: str) -> None:
    if not isinstance(scope, str) or scope not in {"effective", "user"}:
        from jiuwenswarm.extensions_v2.event_types.config import ConfigExtensionError

        raise ConfigExtensionError("UNSUPPORTED_SCOPE")


def read_values(scope: str) -> dict[str, bool | str]:
    """Resolve once, then project only registered fields; never return raw config."""
    from jiuwenswarm.common.config import get_config, get_config_raw

    require_scope(scope)
    config = get_config() if scope == "effective" else get_config_raw()
    return {field.key: field.read(config) for field in public_fields()}


def public_fields() -> tuple[ConfigField, ...]:
    from jiuwenswarm.enterprise.edition import is_enterprise

    # Enterprise locale is owned by Gateway/user preferences, not host YAML.
    return tuple(field for field in FIELDS if field.key != "preferred_language" or not is_enterprise())


def project_snapshot(values: Mapping) -> dict:
    result = {}
    for field in FIELDS:
        if field.key not in values:
            continue
        target = result
        for part in field.path[:-1]:
            target = target.setdefault(part, {})
        target[field.path[-1]] = values[field.key]
    return result
