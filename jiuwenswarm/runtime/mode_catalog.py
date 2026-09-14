# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Stable, single-Agent Runtime mode capability catalog."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from jiuwenswarm.common.mode_matrix import (
    NEW_AGENT_CODE_NORMAL,
    NEW_AGENT_CODE_PLAN,
    NEW_AGENT_WORK_NORMAL,
    NEW_AGENT_WORK_PLAN,
    deprecate_mode,
)

RuntimeWorkMode = Literal["work", "code"]


class ModeCatalogError(RuntimeError):
    """A stable mode catalog lookup failure."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeModeDescriptor:
    """One supported single-Agent mode and its SDK-visible capabilities."""

    mode: str
    work_mode: RuntimeWorkMode
    is_plan: bool
    supports_custom_agent_definitions: bool

    def to_dict(self) -> dict[str, Any]:
        """Return a fresh representation suitable for machine protocols."""
        return {
            "mode": self.mode,
            "work_mode": self.work_mode,
            "is_plan": self.is_plan,
            "supports_custom_agent_definitions": self.supports_custom_agent_definitions,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ModeCatalogResult:
    """A deterministically ordered set of supported Runtime modes."""

    modes: tuple[RuntimeModeDescriptor, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"modes": [item.to_dict() for item in self.modes]}


_SINGLE_AGENT_MODES = (
    RuntimeModeDescriptor(
        mode=NEW_AGENT_WORK_NORMAL,
        work_mode="work",
        is_plan=False,
        supports_custom_agent_definitions=False,
    ),
    RuntimeModeDescriptor(
        mode=NEW_AGENT_WORK_PLAN,
        work_mode="work",
        is_plan=True,
        supports_custom_agent_definitions=False,
    ),
    RuntimeModeDescriptor(
        mode=NEW_AGENT_CODE_NORMAL,
        work_mode="code",
        is_plan=False,
        supports_custom_agent_definitions=True,
    ),
    RuntimeModeDescriptor(
        mode=NEW_AGENT_CODE_PLAN,
        work_mode="code",
        is_plan=True,
        supports_custom_agent_definitions=True,
    ),
)


def list_mode_capabilities() -> ModeCatalogResult:
    """Return only canonical single-Agent modes in a stable product order."""
    return ModeCatalogResult(modes=_SINGLE_AGENT_MODES)


def resolve_mode_capability(requested: object) -> RuntimeModeDescriptor:
    """Resolve a canonical mode while retaining supported legacy aliases."""
    normalized = deprecate_mode(requested)
    target = normalized.strip() if isinstance(normalized, str) else ""
    if not target:
        raise ModeCatalogError("mode is required", code="BAD_REQUEST")
    selected = next((item for item in _SINGLE_AGENT_MODES if item.mode == target), None)
    if selected is None:
        raise ModeCatalogError("single-Agent mode not found", code="NOT_FOUND")
    return selected


__all__ = [
    "ModeCatalogError",
    "ModeCatalogResult",
    "RuntimeModeDescriptor",
    "RuntimeWorkMode",
    "list_mode_capabilities",
    "resolve_mode_capability",
]
