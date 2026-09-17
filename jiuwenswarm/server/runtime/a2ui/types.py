# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared A2UI runtime data structures."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


@dataclass(frozen=True)
class A2UIResponsePart:
    """One parsed segment from a mixed text and A2UI assistant response."""

    kind: Literal["text", "a2ui"]
    text: str = ""
    messages: list[dict[str, Any]] | None = None


@dataclass(frozen=True)
class A2UIExample:
    """Loaded A2UI example used to ground prompt instructions."""

    name: str
    path: Path
    messages: list[dict[str, Any]]


@dataclass(frozen=True)
class A2UIValidationResult:
    """Validation outcome for model-emitted A2UI content."""

    valid: bool
    error: str = ""
