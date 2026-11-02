# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Rendering observations shared by the Claude-aligned session events."""

from dataclasses import dataclass
from typing import Literal


RenderSurface = Literal["terminal", "desktop", "mobile", "vscode"]


@dataclass(frozen=True, slots=True)
class RenderViewport:
    """A host-reported viewport measured in character cells, never pixels."""

    columns: int
    rows: int
    isFullscreen: bool | None = None


def valid_surface(value: object) -> bool:
    """Accept only the rendering surface names declared by Claude Mods."""
    return type(value) is str and value in {"terminal", "desktop", "mobile", "vscode"}


def valid_viewport(value: object) -> bool:
    """Validate optional dimensions without inventing an unknown measurement."""
    return value is None or (
        type(value) is RenderViewport
        and type(value.columns) is int and value.columns > 0
        and type(value.rows) is int and value.rows > 0
        and (value.isFullscreen is None or type(value.isFullscreen) is bool)
    )
