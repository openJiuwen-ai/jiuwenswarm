# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Sandbox operation-context extraction for auto permissions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class SandboxDescriptor:
    """Parser roots exposed by the current sandbox operation."""

    execution_workspace_root: str = ""
    python_package_venv_root: str = ""


def build_sandbox_profile(sys_operation: Any | None = None) -> SandboxDescriptor:
    """Extract parser roots without interpreting sandbox capabilities."""
    if sys_operation is None:
        return SandboxDescriptor()

    return SandboxDescriptor(
        execution_workspace_root=(
            _raw_string_attr(sys_operation, "sandbox_execution_workspace_root") or ""
        ),
        python_package_venv_root=(
            _raw_string_attr(sys_operation, "sandbox_python_package_venv_root") or ""
        ),
    )


def _raw_string_attr(source: Any, name: str) -> str | None:
    value = _value_get(source, name)
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized if normalized else None


def _value_get(source: Any, name: str) -> Any:
    if source is None:
        return None
    if isinstance(source, Mapping):
        return source.get(name)
    return getattr(source, name, None)
