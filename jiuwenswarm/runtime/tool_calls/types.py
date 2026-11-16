"""Identity, execution and protocol ports for one duplex connection."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class ToolCall:
    backend_session_id: str
    generation: str
    call_id: str
    name: str
    arguments: dict[str, Any]

    @property
    def command_id(self) -> str:
        identity = json.dumps([self.backend_session_id, self.generation, self.call_id])
        return "duplex:" + hashlib.sha256(identity.encode()).hexdigest()

    @property
    def signature(self) -> str:
        return json.dumps([self.name, self.arguments], sort_keys=True, allow_nan=False)


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    owner_id: str
    scope: str


@dataclass(frozen=True, slots=True)
class ToolResult:
    success: bool
    content: str = ""


@dataclass(frozen=True, slots=True)
class ToolEvent:
    kind: str
    call_id: str = ""
    call: ToolCall | None = None


class ToolExecution(Protocol):
    async def wait(self) -> ToolResult: ...

    async def cancel(self, reason: str) -> None: ...


class ToolExecutor(Protocol):
    async def start(self, call: ToolCall, context: ToolExecutionContext) -> ToolExecution: ...


class ToolCallAdapter(Protocol):
    def normalize(self, event: Mapping[str, object]) -> tuple[ToolEvent, ...]: ...

    async def submit_result(self, call: ToolCall, result: ToolResult) -> bool: ...

    async def cancel_call(self, call: ToolCall, reason: str) -> None: ...

    async def continue_after_result(self, call: ToolCall) -> None: ...

    async def close(self) -> None: ...

