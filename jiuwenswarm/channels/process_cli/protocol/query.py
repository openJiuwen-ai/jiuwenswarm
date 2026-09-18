# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Strict read-only commands; no Session or Agent is created for a query."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from jiuwenswarm.channels.process_cli.protocol.model import (
    JsonObject,
    RunStatus,
    RuntimeErrorInfo,
    WorkspaceSpec,
    _freeze_object,
    _non_negative_integer,
    _optional_text,
    _positive_number,
    _required_text,
    _strict_object,
    _thaw_json,
)
from jiuwenswarm.channels.process_cli.protocol.version import (
    CURRENT_SCHEMA_VERSION,
    require_supported_schema_version,
)

QUERY_FIELDS = {
    "session.get": ({"session_id"}, {"session_id"}),
    "session.list": ({"limit", "offset"}, set()),
    "model.list": (set(), set()),
    "model.resolve": ({"requested"}, {"requested"}),
    "mode.list": (set(), set()),
    "mode.resolve": ({"requested"}, {"requested"}),
    "permission.get": ({"session_id"}, set()),
    "mcp.validate": ({"references"}, {"references"}),
}


@dataclass(frozen=True, slots=True, kw_only=True)
class OneShotQueryInput:
    """One document, one Runtime lifetime, one read-only operation."""

    operation: str
    params: JsonObject = field(default_factory=dict)
    request_id: str | None = None
    workspace: WorkspaceSpec | None = None
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.operation, str) or self.operation not in QUERY_FIELDS:
            raise ValueError("query operation is not supported")
        allowed, required = QUERY_FIELDS[self.operation]
        params = _strict_object(
            "params",
            self.params,
            allowed=frozenset(allowed),
            required=frozenset(required),
        )
        for key in ("session_id", "requested"):
            if key in params:
                _required_text(key, params[key])
        if "limit" in params:
            limit = _non_negative_integer("limit", params["limit"])
            if not 1 <= limit <= 200:
                raise ValueError("limit must be between 1 and 200")
        if "offset" in params:
            _non_negative_integer("offset", params["offset"])
        if "references" in params:
            references = params["references"]
            if not isinstance(references, (list, tuple)):
                raise ValueError("references must be an array")
            for item in references:
                _required_text("reference", item)
        object.__setattr__(self, "params", _freeze_object("params", params))
        object.__setattr__(
            self, "request_id", _optional_text("request_id", self.request_id)
        )
        if self.workspace is not None and not isinstance(self.workspace, WorkspaceSpec):
            raise TypeError("workspace must be WorkspaceSpec")
        if self.timeout_seconds is not None:
            object.__setattr__(
                self,
                "timeout_seconds",
                _positive_number("timeout_seconds", self.timeout_seconds),
            )

    @classmethod
    def from_dict(cls, value: object) -> OneShotQueryInput:
        data = _strict_object(
            "query",
            value,
            allowed=frozenset(
                {
                    "schema_version",
                    "type",
                    "operation",
                    "params",
                    "request_id",
                    "workspace",
                    "timeout_seconds",
                }
            ),
            required=frozenset({"schema_version", "type", "operation"}),
        )
        require_supported_schema_version(data["schema_version"])
        if data["type"] != "query":
            raise ValueError("query type must be query")
        workspace = data.get("workspace")
        return cls(
            operation=data["operation"],
            params=data.get("params", {}),
            request_id=data.get("request_id"),
            timeout_seconds=data.get("timeout_seconds"),
            workspace=WorkspaceSpec.from_dict(workspace)
            if workspace is not None
            else None,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class OneShotQueryResult:
    """A terminal query outcome, published only after Runtime shutdown."""

    request_id: str
    operation: str | None = None
    status: str = RunStatus.COMPLETED
    exit_code: int = 0
    data: JsonObject | None = None
    error: RuntimeErrorInfo | None = None

    def __post_init__(self) -> None:
        _required_text("request_id", self.request_id)
        status = RunStatus(self.status)
        _non_negative_integer("exit_code", self.exit_code)
        if status == RunStatus.COMPLETED:
            if self.exit_code != 0 or self.error is not None:
                raise ValueError(
                    "successful query must have exit_code zero and no error"
                )
            if self.operation not in QUERY_FIELDS or self.data is None:
                raise ValueError("successful query requires an operation and data")
        elif self.exit_code == 0 or self.error is None:
            raise ValueError("failed query requires nonzero exit_code and error")
        if self.data is not None:
            object.__setattr__(self, "data", _freeze_object("data", self.data))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "type": "query_result",
            "sequence": 0,
            "request_id": self.request_id,
            "session_id": None,
            "operation": self.operation,
            "status": self.status,
            "exit_code": self.exit_code,
            "data": _thaw_json(self.data) if self.data is not None else None,
            "error": self.error.to_dict() if self.error is not None else None,
        }
