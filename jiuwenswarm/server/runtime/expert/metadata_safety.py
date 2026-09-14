# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Canonical safety helpers for expert metadata used in runtime prompts."""

from __future__ import annotations

import re
import unicodedata
from typing import Any


_PROMPT_IDENTIFIER = re.compile(r"^[\w.-]+$", re.UNICODE)
_MEDIA_TYPE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,63}/"
    r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,127}$"
)
_SCHEMA_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/#@+-]{0,255}$")
_STRUCTURAL_TRANSLATION = str.maketrans(
    {
        "<": "＜",
        ">": "＞",
        "`": "｀",
        "|": "｜",
    }
)


def is_safe_prompt_identifier(value: Any, *, max_length: int = 128) -> bool:
    """Return whether *value* is safe as both an ID and an inline prompt token."""

    if not isinstance(value, str) or not value or value != value.strip():
        return False
    if value in {".", ".."} or len(value) > max_length:
        return False
    if any(unicodedata.category(char).startswith("C") for char in value):
        return False
    return _PROMPT_IDENTIFIER.fullmatch(value) is not None


def normalize_untrusted_text(
    value: Any,
    *,
    fallback: str = "",
    limit: int,
) -> str:
    """Normalize display metadata without letting it break prompt data boundaries.

    This function is intentionally not presented as a complete prompt-injection
    defense.  It removes structural/control channels; callers must additionally
    place the result inside a clearly labelled untrusted-data container.
    """

    raw = str(value or "")
    normalized = unicodedata.normalize("NFKC", raw)
    characters: list[str] = []
    for char in normalized:
        if char.isspace() or unicodedata.category(char).startswith("C"):
            characters.append(" ")
        else:
            characters.append(char)
    clean = " ".join("".join(characters).split()).translate(_STRUCTURAL_TRANSLATION)
    if not clean and fallback:
        clean = normalize_untrusted_text(fallback, limit=limit)
    return clean[:limit]


def normalize_media_type(value: Any) -> str | None:
    """Return one canonical MIME token, or ``None`` for unsafe metadata."""

    if value in (None, ""):
        return ""
    if not isinstance(value, str) or value != value.strip():
        return None
    return value.casefold() if _MEDIA_TYPE.fullmatch(value) else None


def normalize_schema_id(value: Any) -> str | None:
    """Return one prompt-safe schema identifier, or ``None`` when invalid."""

    if value in (None, ""):
        return ""
    if not isinstance(value, str) or value != value.strip():
        return None
    return value if _SCHEMA_ID.fullmatch(value) else None


__all__ = [
    "is_safe_prompt_identifier",
    "normalize_media_type",
    "normalize_schema_id",
    "normalize_untrusted_text",
]
