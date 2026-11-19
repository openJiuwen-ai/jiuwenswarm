# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Rendering observations shared by the Claude-aligned session events."""

from dataclasses import field, make_dataclass
from typing import Literal


RenderSurface = Literal["terminal", "desktop", "mobile", "vscode"]


RenderViewport = make_dataclass(
    "RenderViewport",
    [
        ("columns", int),
        ("rows", int),
        ("isFullscreen", bool | None, field(default=None)),
    ],
    namespace={
        "__doc__": "A host-reported viewport measured in character cells, never pixels.",
        "__module__": __name__,
    },
    frozen=True,
    slots=True,
)


def valid_surface(value: object) -> bool:
    """Accept only the rendering surface names declared by Claude Mods."""
    return isinstance(value, str) and value.__class__ is str and value in {
        "terminal", "desktop", "mobile", "vscode",
    }


def valid_viewport(value: object) -> bool:
    """Validate optional dimensions without inventing an unknown measurement."""
    if value is None:
        return True
    if not isinstance(value, RenderViewport) or value.__class__ is not RenderViewport:
        return False
    if not isinstance(value.columns, int) or value.columns.__class__ is not int:
        return False
    if not isinstance(value.rows, int) or value.rows.__class__ is not int:
        return False
    if value.isFullscreen is not None and not isinstance(value.isFullscreen, bool):
        return False
    return value.columns > 0 and value.rows > 0
