# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Controlled appearance: object validation and per-connection providers (S_08).

The browser copy of the validation lives in ``features/webui/appearance``; both
sides run the shared cases in ``gatewayAppearanceCases.json``.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any

APPEARANCE_FIELDS = frozenset({"accent", "surface", "surfaceOpacity", "tokens"})
SURFACE_MODES = frozenset({"opaque", "translucent"})
#: Decorative variables a package may set directly; text and surfaces stay host-owned.
TOKEN_NAMES = frozenset({
    "--color-scrollbar-thumb",
    "--color-sidebar-nav-active",
    "--color-sidebar-nav-active-hover",
    "--color-conversation-active-surface",
    "--color-chat-accent-subtle",
    "--color-action-primary-subtle",
    "--color-action-primary-glow",
    "--color-border-accent",
})
MIN_SURFACE_OPACITY = 0.3

_ACCENT = re.compile(r"#[0-9A-Fa-f]{6}")
_HEX_COLOR = re.compile(r"#(?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})")
_CHANNEL = r" *([0-9]{1,3}) *"
_ALPHA = r" *(0(?:\.[0-9]+)?|1(?:\.0+)?|\.[0-9]+) *"
_RGB = re.compile(rf"rgb\({_CHANNEL},{_CHANNEL},{_CHANNEL}\)")
_RGBA = re.compile(rf"rgba\({_CHANNEL},{_CHANNEL},{_CHANNEL},{_ALPHA}\)")


def valid_color(value: Any) -> bool:
    """Accept hex colors and rgb()/rgba() with integer channels up to 255."""
    if not isinstance(value, str):
        return False
    if _HEX_COLOR.fullmatch(value):
        return True
    match = _RGB.fullmatch(value) or _RGBA.fullmatch(value)
    return match is not None and all(int(channel) <= 255 for channel in match.groups()[:3])


def validate_appearance(value: Any) -> dict[str, Any]:
    """Return a plain copy of a valid appearance object or raise ``ValueError``."""
    if not isinstance(value, Mapping) or not value:
        raise ValueError("appearance must be a non-empty object")
    unknown = set(value) - APPEARANCE_FIELDS
    if unknown:
        raise ValueError(f"unknown appearance fields: {sorted(unknown)}")
    result: dict[str, Any] = {}
    if "accent" in value:
        if not isinstance(value["accent"], str) or not _ACCENT.fullmatch(value["accent"]):
            raise ValueError("accent must be #RRGGBB")
        result["accent"] = value["accent"]
    if "surface" in value:
        if value["surface"] not in SURFACE_MODES:
            raise ValueError("surface must be 'opaque' or 'translucent'")
        result["surface"] = value["surface"]
    if "surfaceOpacity" in value:
        opacity = value["surfaceOpacity"]
        if (type(opacity) not in (int, float) or not math.isfinite(opacity)
                or not MIN_SURFACE_OPACITY <= opacity <= 1):
            raise ValueError("surfaceOpacity must be between 0.3 and 1")
        if value.get("surface") != "translucent":
            raise ValueError("surfaceOpacity requires surface 'translucent'")
        result["surfaceOpacity"] = opacity
    if "tokens" in value:
        tokens = value["tokens"]
        if not isinstance(tokens, Mapping) or not tokens:
            raise ValueError("tokens must be a non-empty object")
        for name, color in tokens.items():
            if name not in TOKEN_NAMES:
                raise ValueError(f"token not allowed: {name}")
            if not valid_color(color):
                raise ValueError(f"invalid color for {name}")
        result["tokens"] = dict(tokens)
    return result


class AppearanceProviders:
    """The appearance provider and frame revision of each browser connection."""

    def __init__(self) -> None:
        self._providers: dict[str, str] = {}
        self._revisions: dict[str, int] = {}
        # Packages whose appearance frame is being sent, per connection: a release must
        # revoke them too, because the frame may still reach the browser.
        self._sending: dict[str, set[str]] = {}

    def provider(self, connection_id: str) -> str | None:
        return self._providers.get(connection_id)

    def revision(self, connection_id: str) -> int:
        return self._revisions.get(connection_id, 0)

    def next_revision(self, connection_id: str) -> int:
        revision = self._revisions.get(connection_id, 0) + 1
        self._revisions[connection_id] = revision
        return revision

    def set(self, connection_id: str, package_id: str | None) -> None:
        if package_id:
            self._providers[connection_id] = package_id
        else:
            self._providers.pop(connection_id, None)

    def begin_send(self, connection_id: str, package_id: str) -> None:
        """Record that a frame of this package is on its way to the connection."""
        self._sending.setdefault(connection_id, set()).add(package_id)

    def finish_send(self, connection_id: str, package_id: str) -> bool:
        """End an in-flight send; False when the package was released meanwhile."""
        packages = self._sending.get(connection_id)
        if not packages or package_id not in packages:
            return False
        packages.discard(package_id)
        if not packages:
            del self._sending[connection_id]
        return True

    def release(self, package_id: str) -> list[tuple[str, int]]:
        """Forget a released package and reserve one revoke revision per connection.

        Connections that only have a frame of the package in flight are revoked as well.
        """
        released = [connection_id for connection_id, owner in self._providers.items() if owner == package_id]
        for connection_id in released:
            del self._providers[connection_id]
        for connection_id, packages in list(self._sending.items()):
            if package_id in packages:
                packages.discard(package_id)
                if not packages:
                    del self._sending[connection_id]
                if connection_id not in released:
                    released.append(connection_id)
        return [(connection_id, self.next_revision(connection_id)) for connection_id in released]

    def drop_connection(self, connection_id: str) -> None:
        self._providers.pop(connection_id, None)
        self._revisions.pop(connection_id, None)
        self._sending.pop(connection_id, None)

    def clear(self) -> None:
        self._providers.clear()
        self._revisions.clear()
        self._sending.clear()
