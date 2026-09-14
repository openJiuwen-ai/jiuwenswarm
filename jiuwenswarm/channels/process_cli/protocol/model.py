# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Value contracts for one Process CLI command and one Runtime lifecycle.

These types describe a future machine-facing adapter. They deliberately do not
implement a resident server, JSON-RPC methods, Session control plane, or host
callbacks. The Process CLI remains the transport owner and converts the shared
Runtime event stream into these records.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, TypeAlias

from jiuwenswarm.channels.process_cli.protocol.version import (
    CURRENT_SCHEMA_VERSION,
    require_supported_schema_version,
)

if TYPE_CHECKING:
    from jiuwenswarm.runtime.events import RuntimeEvent


JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]
JsonObject: TypeAlias = Mapping[str, JsonValue]

_AGENT_NAME = re.compile(r"[A-Za-z0-9_-]{3,50}\Z")


class SingleAgentMode(str, Enum):
    """Canonical modes frozen into the first single-Agent machine schema."""

    WORK_NORMAL = "agent.work.normal"
    WORK_PLAN = "agent.work.plan"
    CODE_NORMAL = "agent.code.normal"
    CODE_PLAN = "agent.code.plan"


class RunStatus(str, Enum):
    """Terminal status of one command process."""

    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


def _required_text(name: str, value: object, *, preserve: bool = False) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value.strip():
        raise ValueError(f"{name} must not be empty")
    return value if preserve else value.strip()


def _optional_text(name: str, value: object) -> str | None:
    if value is None:
        return None
    return _required_text(name, value)


def _optional_raw_text(name: str, value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    return value


def _bool(name: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be a boolean")
    return value


def _non_negative_integer(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must not be negative")
    return value


def _positive_number(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be finite and greater than zero")
    return result


def _string_tuple(name: str, value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{name} must be a sequence of strings")
    result = tuple(
        _required_text(f"{name}[{index}]", item) for index, item in enumerate(value)
    )
    if len(set(result)) != len(result):
        raise ValueError(f"{name} must not contain duplicates")
    return result


def _strict_object(
    name: str,
    value: object,
    *,
    allowed: frozenset[str],
    required: frozenset[str] = frozenset(),
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be an object")
    keys = set(value)
    if any(not isinstance(key, str) for key in keys):
        raise TypeError(f"{name} keys must be strings")
    unknown = sorted(keys - allowed)
    if unknown:
        raise ValueError(f"{name} contains unknown fields: {', '.join(unknown)}")
    missing = sorted(required - keys)
    if missing:
        raise ValueError(f"{name} is missing required fields: {', '.join(missing)}")
    return value


def _freeze_json(
    name: str,
    value: object,
    ancestors: set[int] | None = None,
) -> JsonValue:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{name} must contain only finite numbers")
        return value

    active = ancestors if ancestors is not None else set()
    marker = id(value)
    if marker in active:
        raise ValueError(f"{name} must not contain reference cycles")
    if isinstance(value, Mapping):
        active.add(marker)
        try:
            frozen: dict[str, JsonValue] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise TypeError(f"{name} keys must be strings")
                frozen[key] = _freeze_json(f"{name}.{key}", item, active)
            return MappingProxyType(frozen)
        finally:
            active.remove(marker)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        active.add(marker)
        try:
            return tuple(
                _freeze_json(f"{name}[{index}]", item, active)
                for index, item in enumerate(value)
            )
        finally:
            active.remove(marker)
    raise TypeError(f"{name} must contain only JSON-compatible values")


def _freeze_object(name: str, value: object) -> JsonObject:
    frozen = _freeze_json(name, value)
    if not isinstance(frozen, Mapping):
        raise TypeError(f"{name} must be an object")
    return frozen


def _thaw_json(value: JsonValue) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentSpec:
    """Declarative references for one root Agent in the shared Runtime.

    ``tools`` follows the existing Agent definition semantics: ``("*",)``
    requests the configured tool set, while an explicit non-empty tuple is an
    allowlist. Runtime policy remains authoritative and may further restrict
    it. This contract excludes Team/Workflow entry modes; the selected Agent
    may still use Runtime-managed internal capabilities.
    """

    name: str
    instructions: str
    description: str | None = None
    model: str | None = None
    tools: tuple[str, ...] = ("*",)
    skills: tuple[str, ...] = ()
    max_iterations: int | None = None

    def __post_init__(self) -> None:
        name = _required_text("name", self.name)
        if _AGENT_NAME.fullmatch(name) is None:
            raise ValueError("name must match [A-Za-z0-9_-]{3,50}")
        object.__setattr__(self, "name", name)
        object.__setattr__(
            self,
            "instructions",
            _required_text("instructions", self.instructions, preserve=True),
        )
        object.__setattr__(
            self,
            "description",
            _optional_text("description", self.description),
        )
        object.__setattr__(self, "model", _optional_text("model", self.model))
        tools = _string_tuple("tools", self.tools)
        if not tools:
            raise ValueError("tools must not be empty; use '*' for configured tools")
        if "*" in tools and tools != ("*",):
            raise ValueError("'*' must be the only tools entry when used")
        object.__setattr__(self, "tools", tools)
        object.__setattr__(self, "skills", _string_tuple("skills", self.skills))
        if self.max_iterations is not None:
            value = _non_negative_integer("max_iterations", self.max_iterations)
            if value == 0:
                raise ValueError("max_iterations must be greater than zero")

    def to_dict(self) -> dict[str, Any]:
        """Return a stable JSON-compatible representation."""

        return {
            "name": self.name,
            "instructions": self.instructions,
            "description": self.description,
            "model": self.model,
            "tools": list(self.tools),
            "skills": list(self.skills),
            "max_iterations": self.max_iterations,
        }

    @classmethod
    def from_dict(cls, value: object) -> AgentSpec:
        """Decode one strict Agent declaration."""

        data = _strict_object(
            "agent",
            value,
            allowed=frozenset(
                {
                    "name",
                    "instructions",
                    "description",
                    "model",
                    "tools",
                    "skills",
                    "max_iterations",
                }
            ),
            required=frozenset({"name", "instructions"}),
        )
        return cls(
            name=data["name"],
            instructions=data["instructions"],
            description=data.get("description"),
            model=data.get("model"),
            tools=data.get("tools", ("*",)),
            skills=data.get("skills", ()),
            max_iterations=data.get("max_iterations"),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkspaceSpec:
    """Filesystem context passed to the existing Runtime security boundary."""

    cwd: str | None = None
    project_dir: str | None = None
    trusted_dirs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "cwd", _optional_text("cwd", self.cwd))
        object.__setattr__(
            self,
            "project_dir",
            _optional_text("project_dir", self.project_dir),
        )
        object.__setattr__(
            self,
            "trusted_dirs",
            _string_tuple("trusted_dirs", self.trusted_dirs),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a stable JSON-compatible workspace."""

        return {
            "cwd": self.cwd,
            "project_dir": self.project_dir,
            "trusted_dirs": list(self.trusted_dirs),
        }

    @classmethod
    def from_dict(cls, value: object) -> WorkspaceSpec:
        """Decode one strict workspace object."""

        data = _strict_object(
            "workspace",
            value,
            allowed=frozenset({"cwd", "project_dir", "trusted_dirs"}),
        )
        return cls(
            cwd=data.get("cwd"),
            project_dir=data.get("project_dir"),
            trusted_dirs=data.get("trusted_dirs", ()),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class OneShotRunInput:  # pylint: disable=too-many-instance-attributes
    """Input for one command process and exactly one Runtime lifecycle.

    ``session_id`` requests reuse of that Runtime Session identity and its
    persisted state when present. Omitting it creates a Session; an omitted
    ``mode`` then resolves to ``agent.code.normal``. For an existing Session an
    omitted ``mode`` and ``workspace`` inherit the persisted Session binding.
    For a new Session an omitted ``workspace`` uses the command process working
    directory, with the project directory following the existing Process CLI
    default. ``agent`` is optional so callers can use the configured default
    Agent; it never selects Team, Workflow, or AutoHarness as the root entry
    mode.
    """

    input: str
    schema_version: str = CURRENT_SCHEMA_VERSION
    request_id: str | None = None
    session_id: str | None = None
    agent: AgentSpec | None = None
    mode: str | None = None
    workspace: WorkspaceSpec | None = None
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "schema_version",
            require_supported_schema_version(self.schema_version),
        )
        object.__setattr__(
            self,
            "input",
            _required_text("input", self.input, preserve=True),
        )
        object.__setattr__(
            self,
            "request_id",
            _optional_text("request_id", self.request_id),
        )
        object.__setattr__(
            self,
            "session_id",
            _optional_text("session_id", self.session_id),
        )
        if self.agent is not None and not isinstance(self.agent, AgentSpec):
            raise TypeError("agent must be an AgentSpec")
        if self.mode is not None:
            try:
                mode = SingleAgentMode(self.mode).value
            except (TypeError, ValueError) as exc:
                raise ValueError(f"unsupported single-Agent mode: {self.mode}") from exc
            object.__setattr__(self, "mode", mode)
        elif self.session_id is None:
            object.__setattr__(self, "mode", SingleAgentMode.CODE_NORMAL.value)
        if self.workspace is not None and not isinstance(self.workspace, WorkspaceSpec):
            raise TypeError("workspace must be a WorkspaceSpec")
        if self.timeout_seconds is not None:
            object.__setattr__(
                self,
                "timeout_seconds",
                _positive_number("timeout_seconds", self.timeout_seconds),
            )

    def to_dict(self) -> dict[str, Any]:
        """Return the complete one-shot input record."""

        return {
            "schema_version": self.schema_version,
            "type": "run",
            "request_id": self.request_id,
            "session_id": self.session_id,
            "input": self.input,
            "agent": self.agent.to_dict() if self.agent is not None else None,
            "mode": self.mode,
            "workspace": (
                self.workspace.to_dict() if self.workspace is not None else None
            ),
            "timeout_seconds": self.timeout_seconds,
        }

    @classmethod
    def from_dict(cls, value: object) -> OneShotRunInput:
        """Decode one strict ``type=run`` record."""

        data = _strict_object(
            "run",
            value,
            allowed=frozenset(
                {
                    "schema_version",
                    "type",
                    "request_id",
                    "session_id",
                    "input",
                    "agent",
                    "mode",
                    "workspace",
                    "timeout_seconds",
                }
            ),
            required=frozenset({"schema_version", "type", "input"}),
        )
        if data["type"] != "run":
            raise ValueError("run.type must be 'run'")
        raw_agent = data.get("agent")
        return cls(
            schema_version=data["schema_version"],
            request_id=data.get("request_id"),
            session_id=data.get("session_id"),
            input=data["input"],
            agent=(AgentSpec.from_dict(raw_agent) if raw_agent is not None else None),
            mode=data.get("mode"),
            workspace=(
                WorkspaceSpec.from_dict(data["workspace"])
                if data.get("workspace") is not None
                else None
            ),
            timeout_seconds=data.get("timeout_seconds"),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeErrorInfo:
    """Transport-safe error details; unknown future codes remain decodable."""

    code: str
    message: str
    retryable: bool = False
    details: JsonObject = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _required_text("code", self.code))
        object.__setattr__(
            self,
            "message",
            _required_text("message", self.message, preserve=True),
        )
        object.__setattr__(self, "retryable", _bool("retryable", self.retryable))
        object.__setattr__(self, "details", _freeze_object("details", self.details))

    def to_dict(self) -> dict[str, Any]:
        """Return a stable JSON-compatible error."""

        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": _thaw_json(self.details),
        }

    @classmethod
    def from_dict(cls, value: object) -> RuntimeErrorInfo:
        """Decode one structured Runtime error."""

        data = _strict_object(
            "error",
            value,
            allowed=frozenset({"code", "message", "retryable", "details"}),
            required=frozenset({"code", "message"}),
        )
        return cls(
            code=data["code"],
            message=data["message"],
            retryable=data.get("retryable", False),
            details=data.get("details", {}),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class OneShotEvent:  # pylint: disable=too-many-instance-attributes
    """Read-only JSONL observation of one shared Runtime event.

    ``event_type`` remains open so a compatible SDK can ignore new observation
    events. ``payload`` is opaque JSON owned by that event type; schema ``0.1``
    stabilizes the envelope rather than every event payload. Transport and
    internal Runtime metadata are intentionally omitted. Only
    :class:`OneShotRunResult` represents the outcome of the command process.
    """

    sequence: int
    request_id: str
    event_type: str
    payload: JsonObject | None
    schema_version: str = CURRENT_SCHEMA_VERSION
    session_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "schema_version",
            require_supported_schema_version(self.schema_version),
        )
        object.__setattr__(
            self,
            "sequence",
            _non_negative_integer("sequence", self.sequence),
        )
        object.__setattr__(
            self,
            "request_id",
            _required_text("request_id", self.request_id),
        )
        object.__setattr__(
            self,
            "event_type",
            _required_text("event_type", self.event_type),
        )
        object.__setattr__(
            self,
            "session_id",
            _optional_text("session_id", self.session_id),
        )
        if self.payload is not None:
            object.__setattr__(self, "payload", _freeze_object("payload", self.payload))
            payload_event_type = self.payload.get("event_type")
            if payload_event_type is not None:
                if not isinstance(payload_event_type, str):
                    raise TypeError("payload.event_type must be a string")
                if payload_event_type and payload_event_type != self.event_type:
                    raise ValueError(
                        "payload.event_type must match the top-level event_type"
                    )

    def to_dict(self) -> dict[str, Any]:
        """Return one JSONL-compatible event record."""

        return {
            "schema_version": self.schema_version,
            "type": "event",
            "sequence": self.sequence,
            "request_id": self.request_id,
            "session_id": self.session_id,
            "event_type": self.event_type,
            "payload": _thaw_json(self.payload) if self.payload is not None else None,
        }

    @classmethod
    def from_runtime_event(
        cls,
        event: RuntimeEvent,
        *,
        sequence: int,
    ) -> OneShotEvent:
        """Adapt one observation without mutating Runtime-owned data.

        Runtime completion and ``ok`` flags are deliberately not copied. The
        one-shot adapter converts the overall execution outcome into exactly
        one terminal :class:`OneShotRunResult` after Runtime cleanup.
        """

        return cls(
            sequence=sequence,
            request_id=event.request_id,
            session_id=event.session_id,
            event_type=event.event_type or "runtime.event",
            payload=event.payload,
        )

    @classmethod
    def from_dict(cls, value: object) -> OneShotEvent:
        """Decode one strict ``type=event`` record."""

        data = _strict_object(
            "event",
            value,
            allowed=frozenset(
                {
                    "schema_version",
                    "type",
                    "sequence",
                    "request_id",
                    "session_id",
                    "event_type",
                    "payload",
                }
            ),
            required=frozenset(
                {"schema_version", "type", "sequence", "request_id", "event_type"}
            ),
        )
        if data["type"] != "event":
            raise ValueError("event.type must be 'event'")
        return cls(
            schema_version=data["schema_version"],
            sequence=data["sequence"],
            request_id=data["request_id"],
            session_id=data.get("session_id"),
            event_type=data["event_type"],
            payload=data.get("payload"),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class OneShotRunResult:  # pylint: disable=too-many-instance-attributes
    """The one and only terminal record emitted before process exit.

    ``exit_code`` is the actual process status. Except for successful runs,
    callers use the structured error rather than deriving semantics from the
    numeric value.
    """

    sequence: int
    request_id: str
    status: str
    exit_code: int
    schema_version: str = CURRENT_SCHEMA_VERSION
    session_id: str | None = None
    output: str | None = None
    error: RuntimeErrorInfo | None = None
    usage: JsonObject = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "schema_version",
            require_supported_schema_version(self.schema_version),
        )
        object.__setattr__(
            self,
            "sequence",
            _non_negative_integer("sequence", self.sequence),
        )
        object.__setattr__(
            self,
            "request_id",
            _required_text("request_id", self.request_id),
        )
        object.__setattr__(
            self,
            "session_id",
            _optional_text("session_id", self.session_id),
        )
        try:
            status = RunStatus(self.status).value
        except (TypeError, ValueError) as exc:
            raise ValueError(f"unsupported run status: {self.status}") from exc
        object.__setattr__(self, "status", status)
        exit_code = _non_negative_integer("exit_code", self.exit_code)
        object.__setattr__(
            self,
            "output",
            _optional_raw_text("output", self.output),
        )
        if self.error is not None and not isinstance(self.error, RuntimeErrorInfo):
            raise TypeError("error must be RuntimeErrorInfo")
        if status == RunStatus.COMPLETED.value:
            if self.session_id is None:
                raise ValueError("completed result must contain a session_id")
            if exit_code != 0:
                raise ValueError("completed result must use exit_code 0")
            if self.error is not None:
                raise ValueError("completed result must not contain an error")
        else:
            if exit_code == 0:
                raise ValueError("non-completed result must use a non-zero exit_code")
            if self.error is None:
                raise ValueError("non-completed result must contain an error")
        object.__setattr__(self, "usage", _freeze_object("usage", self.usage))

    def to_dict(self) -> dict[str, Any]:
        """Return the terminal JSONL-compatible result record."""

        return {
            "schema_version": self.schema_version,
            "type": "result",
            "sequence": self.sequence,
            "request_id": self.request_id,
            "session_id": self.session_id,
            "status": self.status,
            "exit_code": self.exit_code,
            "output": self.output,
            "error": self.error.to_dict() if self.error is not None else None,
            "usage": _thaw_json(self.usage),
        }

    @classmethod
    def from_dict(cls, value: object) -> OneShotRunResult:
        """Decode one strict ``type=result`` record."""

        data = _strict_object(
            "result",
            value,
            allowed=frozenset(
                {
                    "schema_version",
                    "type",
                    "sequence",
                    "request_id",
                    "session_id",
                    "status",
                    "exit_code",
                    "output",
                    "error",
                    "usage",
                }
            ),
            required=frozenset(
                {
                    "schema_version",
                    "type",
                    "sequence",
                    "request_id",
                    "status",
                    "exit_code",
                }
            ),
        )
        if data["type"] != "result":
            raise ValueError("result.type must be 'result'")
        raw_error = data.get("error")
        return cls(
            schema_version=data["schema_version"],
            sequence=data["sequence"],
            request_id=data["request_id"],
            session_id=data.get("session_id"),
            status=data["status"],
            exit_code=data["exit_code"],
            output=data.get("output"),
            error=(
                RuntimeErrorInfo.from_dict(raw_error) if raw_error is not None else None
            ),
            usage=data.get("usage", {}),
        )
