"""Smart-only, invocation-local projection of verified native file accesses."""

from __future__ import annotations

import json
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class NativePathAccess:
    """Execution identity and one literal file or bounded search base."""

    tool_name: str
    arguments_json: str
    path: str
    action: str

    @property
    def guard_tool(self) -> str:
        return "write_file" if self.action == "write" else "read_file"

    @property
    def guard_args(self) -> dict[str, str]:
        return {"file_path": self.path}


NATIVE_PATH_ACCESS: ContextVar[NativePathAccess | None] = ContextVar(
    "smart_native_path_access", default=None,
)


def native_arguments_json(args: Mapping[str, Any]) -> str:
    return json.dumps(dict(args), sort_keys=True, ensure_ascii=True, allow_nan=False)


def current_native_path_access(
    tool_name: str, args: Mapping[str, Any],
) -> NativePathAccess | None:
    access = NATIVE_PATH_ACCESS.get()
    if access is None or tool_name != access.tool_name:
        return None
    try:
        return access if native_arguments_json(args) == access.arguments_json else None
    except (TypeError, ValueError):
        return None


class NativePathGuardProjection:
    """Adapt only extraction; the installed SDK checker still owns path policy.

    Installed only on Smart's base rail. Tool-level policy and scene hooks keep
    the real tool name and arguments. No global SDK registry is modified.
    """

    def __init__(self, checker: Any) -> None:
        self.checker = checker

    def evaluate(self, tool_name: str, tool_args: Mapping[str, Any]) -> Any:
        access = current_native_path_access(tool_name, tool_args)
        if access is not None:
            return self.checker.evaluate(access.guard_tool, access.guard_args)
        return self.checker.evaluate(tool_name, tool_args)
