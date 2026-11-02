# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Resolve untrusted steer image references inside the owning session uploads."""

from __future__ import annotations

import io
import os
import stat
import warnings
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from gateway.common.attachment_safety import image_suffix_for_mime, safe_session_dirname, safe_upload_filename
from jiuwenswarm.common.utils import get_agent_sessions_dir
from jiuwenswarm.runtime.session_input import validate_steer_image_references

MAX_STEER_IMAGE_BYTES = 10 * 1024 * 1024
MAX_STEER_IMAGE_PIXELS = 40_000_000
_IMAGE_FORMAT_MIME_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif"}


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


def _checked_stat(path: Path, *, directory: bool = False) -> os.stat_result:
    info = path.lstat()
    is_reparse = bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    if stat.S_ISLNK(info.st_mode) or is_reparse:
        raise ValueError("steer image paths must not contain symlinks or junctions")
    if directory and not stat.S_ISDIR(info.st_mode):
        raise ValueError("session uploads path is not a directory")
    if not directory and not stat.S_ISREG(info.st_mode):
        raise ValueError("steer image path must identify a regular file")
    return info


def _image_mime_type(data: bytes) -> str:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                mime_type = _IMAGE_FORMAT_MIME_TYPES.get(image.format or "")
                if mime_type is None:
                    raise ValueError("unsupported steer image format")
                if image.width * image.height > MAX_STEER_IMAGE_PIXELS:
                    raise ValueError(f"steer image exceeds {MAX_STEER_IMAGE_PIXELS} pixels")
                image.verify()
            # verify() checks container integrity; load() also rejects a
            # truncated first frame before an Agent is told it can read it.
            with Image.open(io.BytesIO(data)) as image:
                image.load()
    except (
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise ValueError("steer image is invalid or exceeds the safe decoded size") from exc
    return mime_type


def _resolve_image(item: Mapping[str, Any], upload_dir: Path) -> dict[str, Any]:
    raw = Path(item["path"])
    if not raw.is_absolute() or ".." in raw.parts or not _same_path(raw.parent, upload_dir):
        raise ValueError("steer image path is outside the current session uploads")
    if raw.name != item["filename"]:
        raise ValueError("steer image filename does not match its uploaded path")
    if raw.name != safe_upload_filename(raw.name, fallback="image"):
        raise ValueError("steer image path is not an uploaded filename")
    info = _checked_stat(raw)
    if info.st_size <= 0 or info.st_size > MAX_STEER_IMAGE_BYTES:
        raise ValueError("steer image must be non-empty and at most 10 MiB")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(raw, flags, mode=0o600)
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino) or not stat.S_ISREG(opened.st_mode):
            raise ValueError("steer image changed during validation; upload it again")
        data = stream.read(MAX_STEER_IMAGE_BYTES + 1)
    if not data or len(data) > MAX_STEER_IMAGE_BYTES:
        raise ValueError("steer image must be non-empty and at most 10 MiB")
    mime_type = _image_mime_type(data)
    suffix = raw.suffix.lower()
    valid_suffix = suffix == image_suffix_for_mime(mime_type) or (
        mime_type == "image/jpeg" and suffix in {".jpeg", ".jfif"}
    )
    if mime_type != item["mime_type"] or not valid_suffix:
        raise ValueError("steer image MIME type and filename must match its actual image format")
    current = _checked_stat(raw)
    if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (
        info.st_dev, info.st_ino, len(data), info.st_mtime_ns,
    ):
        raise ValueError("steer image changed during validation; upload it again")
    return {
        "type": "image", "path": str(raw), "filename": raw.name,
        "mime_type": mime_type, "size_bytes": len(data),
    }


def resolve_steer_image_references(params: Mapping[str, Any], session_id: str | None) -> list[dict[str, Any]]:
    """Validate and canonicalize uploaded images; call off the async event loop.

    Only ordinary files directly under this session's uploads are eligible.
    Client filenames and MIME types are checked; file sizes come from disk.
    """
    references = validate_steer_image_references(params)
    if not references:
        return []
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("image steering requires a current session")
    sessions_dir = get_agent_sessions_dir().absolute()
    session_dir = sessions_dir / safe_session_dirname(session_id)
    upload_dir = session_dir / "uploads"
    try:
        session_info = _checked_stat(session_dir, directory=True)
        upload_info = _checked_stat(upload_dir, directory=True)
        images = [_resolve_image(item, upload_dir) for item in references]
        for directory, original in ((session_dir, session_info), (upload_dir, upload_info)):
            current = _checked_stat(directory, directory=True)
            if (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino):
                raise ValueError("session uploads changed during validation; upload images again")
    except (OSError, RuntimeError) as exc:
        raise ValueError("steer image upload is missing or cannot be read; upload it again") from exc
    return images
