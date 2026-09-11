# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Transport-neutral contracts for compacting one Runtime Session context."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, TypeAlias

from jiuwenswarm.runtime.events import RuntimeEvent

ContextCompactStatus: TypeAlias = Literal["busy", "compressed", "noop"]


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextCompactInput:
    """Identity and routing facts required by Runtime context compaction."""

    request_id: str
    channel_id: str
    session_id: str
    mode: str = "agent"
    project_dir: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextCompactResult:
    """Compaction outcome plus transport-neutral state events."""

    result: ContextCompactStatus | None
    stats: dict[str, Any] | None
    summary: str = ""
    events: tuple[RuntimeEvent, ...] = ()


__all__ = [
    "ContextCompactInput",
    "ContextCompactResult",
    "ContextCompactStatus",
]
