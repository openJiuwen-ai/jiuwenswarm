# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Read application icons from local files."""

import base64
from pathlib import Path
from urllib.parse import unquote, urlsplit

_MAX_ICON_BYTES = 256 * 1024
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_ICON_TYPES = {
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def read_local_app_icon(root: Path, href: str) -> str:
    """Return a bounded data URL for an icon contained in the app directory."""
    if not href:
        return ""
    try:
        url = urlsplit(href)
        if url.scheme or url.netloc:
            return ""
        root = root.resolve()
        icon = (root / unquote(url.path).lstrip("/")).resolve()
        mime = _ICON_TYPES.get(icon.suffix.lower())
        if not mime or not icon.is_relative_to(root) or not icon.is_file():
            return ""
        with icon.open("rb") as source:
            data = source.read(_MAX_ICON_BYTES + 1)
    except (OSError, RuntimeError, ValueError):
        return ""
    if not data or len(data) > _MAX_ICON_BYTES:
        return ""
    if mime == "image/png" and not data.startswith(_PNG_SIGNATURE):
        return ""
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
