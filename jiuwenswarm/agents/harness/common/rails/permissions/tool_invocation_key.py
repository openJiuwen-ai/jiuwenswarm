# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Host-owned identity and lifecycle state for one logical tool invocation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, TypeAlias

TOOL_INVOCATION_KEY_VERSION = 1
MAX_TOOL_INVOCATION_ID_LENGTH = 128

ExecutorKind: TypeAlias = Literal["agent"]


def normalize_tool_invocation_text(name: str, value: Any, *, maximum: int = 512) -> str:
    """Return one normalized, bounded identity field or raise ``ValueError``."""

    normalized = value.strip() if isinstance(value, str) else ""
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"invalid {name}")
    return normalized


@dataclass(frozen=True)
class ToolInvocationKeyV1:
    """Additive wire identity shared by all events for one invocation."""

    invocation_id: str
    root_session_id: str
    request_id: str
    executor_kind: ExecutorKind
    execution_session_id: str
    tool_call_id: str
    version: Literal[1] = TOOL_INVOCATION_KEY_VERSION

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != TOOL_INVOCATION_KEY_VERSION:
            raise ValueError("unsupported tool invocation key version")
        if self.executor_kind != "agent":
            raise ValueError("invalid executor_kind")
        object.__setattr__(
            self,
            "invocation_id",
            normalize_tool_invocation_text(
                "invocation_id",
                self.invocation_id,
                maximum=MAX_TOOL_INVOCATION_ID_LENGTH,
            ),
        )
        for field_name in (
            "root_session_id",
            "request_id",
            "execution_session_id",
            "tool_call_id",
        ):
            object.__setattr__(
                self,
                field_name,
                normalize_tool_invocation_text(field_name, getattr(self, field_name)),
            )

    def to_wire(self) -> dict[str, Any]:
        """Return the versioned additive event representation."""

        return {
            "version": self.version,
            "invocation_id": self.invocation_id,
            "root_session_id": self.root_session_id,
            "request_id": self.request_id,
            "executor_kind": self.executor_kind,
            "execution_session_id": self.execution_session_id,
            "tool_call_id": self.tool_call_id,
        }

    @classmethod
    def from_wire(cls, value: Mapping[str, Any]) -> ToolInvocationKeyV1:
        """Parse a complete V1 event mapping without accepting partial identity."""

        if not isinstance(value, Mapping):
            raise ValueError("tool invocation key must be a mapping")
        required = {
            "version",
            "invocation_id",
            "root_session_id",
            "request_id",
            "executor_kind",
            "execution_session_id",
            "tool_call_id",
        }
        if set(value) != required:
            raise ValueError("invalid tool invocation key fields")
        return cls(**dict(value))
