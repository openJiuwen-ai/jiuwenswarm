# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bridge native Shell evidence across the SDK dependency upgrade."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Protocol

try:
    from openjiuwen.harness.security.permission_engine.shell_path_context import (
        current_shell_path_analysis as _core_current_shell_paths,
        shell_path_call as _core_shell_path_call,
    )
except ModuleNotFoundError as exc:
    if exc.name != "openjiuwen.harness.security.permission_engine.shell_path_context":
        raise
    _core_current_shell_paths = None
    _core_shell_path_call = None
else:
    if not callable(_core_current_shell_paths) or not callable(_core_shell_path_call):
        raise ImportError("Core shell path capability requires two callable entry points")

if TYPE_CHECKING:
    from openjiuwen.harness.security.permission_engine.fileguard.shell_paths import ShellPathAnalysis


class ShellPathScope(Protocol):
    def bind(self, tool: Any, args: dict[str, Any]) -> None:
        """Bind evidence for the trusted tool and its final execution arguments."""


@contextmanager
def shell_path_call() -> Iterator[ShellPathScope | None]:
    """Enter the SDK evidence scope when the capability is available."""
    if _core_shell_path_call is None:
        yield None
        return
    with _core_shell_path_call() as scope:
        if not callable(getattr(scope, "bind", None)):
            raise RuntimeError("Core shell path scope does not provide bind()")
        yield scope


def current_shell_paths(name: str, args: Mapping[str, Any]) -> ShellPathAnalysis | None:
    """Look up the SDK result for these exact shell arguments."""
    if _core_current_shell_paths is None:
        return None
    return _core_current_shell_paths(name, args)


def shell_path_digest(name: str, args: Mapping[str, Any]) -> str | None:
    """Fingerprint a pending approval's executor evidence without storing raw paths."""
    analysis = current_shell_paths(name, args)
    if analysis is None:
        return None
    return hashlib.sha256(repr(analysis).encode("utf-8")).hexdigest()
