# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Registration surface handed to one Gateway extension package.

Gateways extensions may only register USER handlers: they observe and replace
platform behaviour, they never own a CORE protocol rule.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

ALLOWED_LEVELS = frozenset({"user"})


@dataclass(frozen=True, slots=True)
class HandlerDeclaration:
    """One handler collected while loading a package; not on the bus yet."""

    event_type: str
    handler: Callable[..., Any]
    match_pattern: Mapping[str, Any]
    local_id: str


class GatewayRegistrar:
    """Collect handler declarations for one package activation."""

    def __init__(self) -> None:
        self.declarations: list[HandlerDeclaration] = []

    def register_event_handler(
        self,
        event_type: object,
        handler: Callable[..., Any],
        match_pattern: object = "*",
        handler_level: object = "user",
    ) -> str:
        """Declare one USER handler and return its package-local id.

        ``match_pattern="*"`` is accepted as a shorthand for "match every
        event"; every other pattern must be a mapping so the same subset rule
        as AgentServer applies.
        """

        level = _level_name(handler_level)
        if level not in ALLOWED_LEVELS:
            raise ValueError(f"gateway extensions may register user handlers only, got {level!r}")
        if not callable(handler):
            raise ValueError("handler must be callable")
        pattern = _match_pattern(match_pattern)
        local_id = f"local-{len(self.declarations) + 1}"
        self.declarations.append(
            HandlerDeclaration(
                event_type=_event_name(event_type),
                handler=handler,
                match_pattern=pattern,
                local_id=local_id,
            )
        )
        return local_id


def _match_pattern(value: object) -> Mapping[str, Any]:
    if value == "*":
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("match_pattern must be '*' or a mapping")
    return dict(value)


def _level_name(value: object) -> str:
    if isinstance(value, str):
        return value.strip().lower()
    inner = getattr(value, "value", None)
    if isinstance(inner, str):
        return inner.strip().lower()
    return str(value).strip().lower()


def _event_name(value: object) -> str:
    if isinstance(value, str):
        return value
    inner = getattr(value, "value", None)
    return inner if isinstance(inner, str) else str(value)
