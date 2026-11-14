# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Serve package avatars through the existing signed HTTP download channel."""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import tempfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from jiuwenswarm.common.file_download_token import build_file_download_info
from jiuwenswarm.common.utils import get_agent_workspace_dir, get_package_avatar_cache_dir

logger = logging.getLogger(__name__)

_IMAGE_SUFFIXES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
_RASTER_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
_SVG_MIME = "image/svg+xml"
# SVG stays an inline data URL: the browser isolates it inside <img>, while
# serving the same bytes from our origin would allow an active document.
# The cap keeps one oversized vector from re-inflating catalog messages.
_MAX_INLINE_SVG_BYTES = 64 * 1024
_AVATAR_URL_TTL_SECONDS = 7 * 24 * 3600


def _cache_avatar(data: bytes, suffix: str) -> str:
    cache = get_package_avatar_cache_dir()
    cache.resolve().relative_to(get_agent_workspace_dir().resolve())
    cache.mkdir(parents=True, exist_ok=True)
    cache = cache.resolve()
    cache.relative_to(get_agent_workspace_dir().resolve())
    target = cache / f"{hashlib.sha256(data).hexdigest()}{suffix}"
    if target.is_symlink():
        return ""
    if not target.is_file():
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=cache, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(data)
            os.replace(temporary, target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    # Content-addressed copies also cover read-only resources and keep URLs
    # valid when a Hub preview's temporary package directory is released.
    # Catalogs stay rendered for hours on the desktop, so a short lifetime breaks
    # images first shown later. Unexpiring tokens need a session ID, which a
    # catalog does not have; the token only names this content-addressed copy.
    info = build_file_download_info(str(target), target.name, expires_in=_AVATAR_URL_TTL_SECONDS)
    return f"{info['download_url']}&inline=1"


def _inline_svg(data: bytes) -> str:
    if not data or len(data) > _MAX_INLINE_SVG_BYTES:
        return ""
    return f"data:{_SVG_MIME};base64,{base64.b64encode(data).decode('ascii')}"


def resolve_package_avatar_url(raw: object, *, package_dir: Path | None = None) -> str:
    """Return a remote URL, a signed HTTP URL for raster images, or a small inline SVG."""
    if not isinstance(raw, str):
        return ""
    value = raw.strip()
    if not value or any(ord(char) < 0x20 for char in value):
        return ""
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() in {"http", "https"} and parsed.netloc:
            return value
        if value.lower().startswith("data:image/"):
            header, encoded = value.split(",", 1)
            mime = header[5:].removesuffix(";base64").lower()
            if not header.endswith(";base64"):
                return ""
            data = base64.b64decode(encoded, validate=True)
            if mime == _SVG_MIME:
                return _inline_svg(data)
            suffix = _IMAGE_SUFFIXES.get(mime)
            if suffix is None:
                return ""
        else:
            relative = value.replace("\\", "/")
            if package_dir is None or relative.startswith("/") or ".." in PurePosixPath(relative).parts:
                return ""
            suffix = Path(relative).suffix.lower()
            if suffix != ".svg" and suffix not in _RASTER_SUFFIXES:
                return ""
            root = package_dir.resolve()
            path = (root / relative).resolve()
            path.relative_to(root)
            data = path.read_bytes()
            if suffix == ".svg":
                return _inline_svg(data)
        return _cache_avatar(data, suffix) if data else ""
    except (OSError, ValueError):
        logger.debug("Unable to resolve package avatar", exc_info=True)
        return ""
