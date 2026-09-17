# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Transport-neutral contracts for one root Agent definition.

This module validates declarative input and describes the part of that input
that the shared Runtime can currently execute.  It deliberately has no
dependency on a Process CLI protocol, a transport, or an Agent implementation.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any


_AGENT_NAME_PATTERN = re.compile(r"[A-Za-z0-9_-]{3,50}\Z")
_DEFINITION_SCHEMA = "jiuwenswarm.runtime.agent-definition.v1"
_DEFINITION_FIELDS = frozenset(
    {
        "name",
        "instructions",
        "description",
        "model",
        "tools",
        "skills",
        "max_iterations",
    }
)
_REQUIRED_DEFINITION_FIELDS = frozenset({"name", "instructions"})


class RuntimeAgentDefinitionErrorCode(str, Enum):
    """Stable machine error codes for the Runtime Agent contract."""

    INVALID_DEFINITION = "AGENT_DEFINITION_INVALID"
    INVALID_REQUEST = "AGENT_DEFINITION_REQUEST_INVALID"
    INVALID_MODE = "AGENT_DEFINITION_MODE_INVALID"
    UNSUPPORTED_MODE = "AGENT_DEFINITION_MODE_UNSUPPORTED"
    MODEL_NOT_FOUND = "AGENT_DEFINITION_MODEL_NOT_FOUND"
    TOOL_ALLOWLIST_UNSUPPORTED = "AGENT_DEFINITION_TOOL_ALLOWLIST_UNSUPPORTED"
    SESSION_CONFLICT = "AGENT_DEFINITION_SESSION_CONFLICT"


class RuntimeAgentDefinitionError(ValueError):
    """Stable, transport-independent Agent-definition failure."""

    retryable = False

    def __init__(
        self,
        message: str,
        *,
        code: RuntimeAgentDefinitionErrorCode,
        field: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code.value
        self.field = field


class RuntimeAgentMode(str, Enum):
    """Canonical modes accepted by the single-root-Agent Runtime contract."""

    WORK_NORMAL = "agent.work.normal"
    WORK_PLAN = "agent.work.plan"
    CODE_NORMAL = "agent.code.normal"
    CODE_PLAN = "agent.code.plan"


def _invalid(field: str | None, message: str) -> RuntimeAgentDefinitionError:
    return RuntimeAgentDefinitionError(
        message,
        code=RuntimeAgentDefinitionErrorCode.INVALID_DEFINITION,
        field=field,
    )


def _required_text(
    field: str,
    value: object,
    *,
    preserve: bool = False,
) -> str:
    if not isinstance(value, str):
        raise _invalid(field, f"{field} must be a string")
    if not value.strip():
        raise _invalid(field, f"{field} must not be empty")
    return value if preserve else value.strip()


def _optional_text(field: str, value: object) -> str | None:
    if value is None:
        return None
    return _required_text(field, value)


def _string_tuple(field: str, value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise _invalid(field, f"{field} must be a sequence of strings")
    result = tuple(
        _required_text(f"{field}[{index}]", item) for index, item in enumerate(value)
    )
    if len(set(result)) != len(result):
        raise _invalid(field, f"{field} must not contain duplicates")
    return result


def _configured_tools(value: object) -> str:
    if value == "*":
        return "*"
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        tools = _string_tuple("tools", value)
        if not tools:
            raise _invalid("tools", "tools must not be empty; use '*' instead")
        if tools == ("*",):
            return "*"
        raise RuntimeAgentDefinitionError(
            "explicit tool allowlists are not supported by this Runtime contract",
            code=RuntimeAgentDefinitionErrorCode.TOOL_ALLOWLIST_UNSUPPORTED,
            field="tools",
        )
    if isinstance(value, str):
        _required_text("tools", value)
        raise RuntimeAgentDefinitionError(
            "explicit tool allowlists are not supported by this Runtime contract",
            code=RuntimeAgentDefinitionErrorCode.TOOL_ALLOWLIST_UNSUPPORTED,
            field="tools",
        )
    raise _invalid("tools", "tools must be '*' or a sequence of strings")


def _strict_definition_mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _invalid(None, "agent definition must be an object")
    if any(not isinstance(key, str) for key in value):
        raise _invalid(None, "agent definition keys must be strings")
    keys = set(value)
    unknown = sorted(keys - _DEFINITION_FIELDS)
    if unknown:
        raise _invalid(
            None,
            f"agent definition contains unknown fields: {', '.join(unknown)}",
        )
    missing = sorted(_REQUIRED_DEFINITION_FIELDS - keys)
    if missing:
        raise _invalid(
            None,
            f"agent definition is missing required fields: {', '.join(missing)}",
        )
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeAgentDefinition:
    """Validated definition of one root Agent owned by the shared Runtime.

    ``tools='*'`` means use the Runtime's configured tool set.  An explicit
    allowlist is intentionally rejected until the Runtime can enforce it after
    all rails and extensions have contributed their tools.
    """

    name: str
    instructions: str
    description: str | None = None
    model: str | None = None
    tools: str = "*"
    skills: tuple[str, ...] = ()
    max_iterations: int | None = None

    def __post_init__(self) -> None:
        name = _required_text("name", self.name)
        if _AGENT_NAME_PATTERN.fullmatch(name) is None:
            raise _invalid("name", "name must match [A-Za-z0-9_-]{3,50}")
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
        object.__setattr__(self, "tools", _configured_tools(self.tools))
        object.__setattr__(self, "skills", _string_tuple("skills", self.skills))
        if self.max_iterations is not None:
            if isinstance(self.max_iterations, bool) or not isinstance(
                self.max_iterations, int
            ):
                raise _invalid("max_iterations", "max_iterations must be an integer")
            if self.max_iterations <= 0:
                raise _invalid(
                    "max_iterations",
                    "max_iterations must be greater than zero",
                )

    @classmethod
    def from_mapping(cls, value: object) -> RuntimeAgentDefinition:
        """Parse one strict, transport-neutral definition mapping."""

        data = _strict_definition_mapping(value)
        return cls(
            name=data["name"],
            instructions=data["instructions"],
            description=data.get("description"),
            model=data.get("model"),
            tools=data.get("tools", "*"),
            skills=data.get("skills", ()),
            max_iterations=data.get("max_iterations"),
        )

    def to_dict(self) -> dict[str, object]:
        """Return the canonical JSON-compatible definition."""

        return {
            "name": self.name,
            "instructions": self.instructions,
            "description": self.description,
            "model": self.model,
            "tools": self.tools,
            "skills": list(self.skills),
            "max_iterations": self.max_iterations,
        }

    @property
    def fingerprint(self) -> str:
        """Return a stable SHA-256 fingerprint of the canonical definition."""

        payload = {
            "schema": _DEFINITION_SCHEMA,
            "definition": self.to_dict(),
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()


def _runtime_agent_mode(value: object) -> RuntimeAgentMode:
    if isinstance(value, RuntimeAgentMode):
        return value
    if isinstance(value, str):
        try:
            return RuntimeAgentMode(value)
        except ValueError:
            pass
    raise RuntimeAgentDefinitionError(
        "unsupported single-Agent mode",
        code=RuntimeAgentDefinitionErrorCode.INVALID_MODE,
        field="mode",
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeAgentExecution:
    """Validated handoff for a supported custom root-Agent execution.

    This is a capability contract, not a second executor.  ``AgentRuntime``
    translates it into the existing AgentManager and Agent Adapter input.
    """

    definition: RuntimeAgentDefinition
    mode: RuntimeAgentMode | str

    def __post_init__(self) -> None:
        if not isinstance(self.definition, RuntimeAgentDefinition):
            raise _invalid("agent", "agent must be a RuntimeAgentDefinition")
        mode = _runtime_agent_mode(self.mode)
        if mode in {RuntimeAgentMode.WORK_NORMAL, RuntimeAgentMode.WORK_PLAN}:
            raise RuntimeAgentDefinitionError(
                "custom Agent definitions are not supported in work mode",
                code=RuntimeAgentDefinitionErrorCode.UNSUPPORTED_MODE,
                field="mode",
            )
        object.__setattr__(self, "mode", mode)

    @property
    def fingerprint(self) -> str:
        """Return the immutable definition fingerprint used by this handoff."""

        return self.definition.fingerprint

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-compatible execution description."""

        return {
            "agent": self.definition.to_dict(),
            "agent_fingerprint": self.fingerprint,
            "mode": _runtime_agent_mode(self.mode).value,
        }


def prepare_agent_execution(
    definition: RuntimeAgentDefinition | Mapping[str, Any],
    *,
    mode: RuntimeAgentMode | str,
) -> RuntimeAgentExecution:
    """Validate and describe a currently supported custom Agent execution."""

    parsed = (
        definition
        if isinstance(definition, RuntimeAgentDefinition)
        else RuntimeAgentDefinition.from_mapping(definition)
    )
    return RuntimeAgentExecution(definition=parsed, mode=mode)


__all__ = [
    "RuntimeAgentDefinition",
    "RuntimeAgentDefinitionError",
    "RuntimeAgentDefinitionErrorCode",
    "RuntimeAgentExecution",
    "RuntimeAgentMode",
    "prepare_agent_execution",
]
