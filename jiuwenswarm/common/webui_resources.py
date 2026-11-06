# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Wire contract for AgentServer-owned, capability-authorized WebUI files."""

from __future__ import annotations

import base64
import binascii
import json
import re
from pathlib import PurePosixPath

RESOURCE_PREFIX = "/extension-assets/_agent"
MAX_RESOURCE_BYTES = 1024 * 1024
MAX_RESOURCE_SET_BYTES = 4 * MAX_RESOURCE_BYTES
MAX_RESOURCE_FILES = 64
RESOURCE_MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".woff2": "font/woff2",
}


def resource_path(value: str) -> str:
    """Validate one canonical relative resource path without filesystem guessing."""
    if (not isinstance(value, str) or not value or len(value) > 512
            or re.fullmatch(r"[A-Za-z0-9._/-]+", value) is None
            or any(part in ("", ".", "..") or part.endswith(".")
                   or re.fullmatch(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part, re.I)
                   for part in value.split("/"))
            or PurePosixPath(value).suffix.lower() not in RESOURCE_MIME_TYPES):
        raise ValueError("Invalid WebUI resource path")
    return value


def encode_resource_route(user_id: str, session_id: str, connection_id: str) -> str:
    """Encode routing facts; the separate random resource ID grants access."""
    raw = json.dumps([user_id, session_id, connection_id], ensure_ascii=False, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_resource_route(value: str) -> tuple[str, str, str]:
    """Reject malformed and noncanonical routing information before E2A dispatch."""
    if not isinstance(value, str) or len(value) > 4096 or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
        raise ValueError("Invalid WebUI resource route")
    try:
        parts = json.loads(base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True))
    except (ValueError, UnicodeError, binascii.Error) as exc:
        raise ValueError("Invalid WebUI resource route") from exc
    if (not isinstance(parts, list) or len(parts) != 3
            or any(not isinstance(part, str) or len(part) > 512 for part in parts)
            or bool(parts[1]) == bool(parts[2]) or encode_resource_route(*parts) != value):
        raise ValueError("Invalid WebUI resource route")
    return parts[0], parts[1], parts[2]
