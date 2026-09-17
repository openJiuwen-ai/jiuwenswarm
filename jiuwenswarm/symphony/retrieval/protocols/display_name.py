from __future__ import annotations

import re


_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_ALNUM_RE = re.compile(r"[^A-Za-z0-9]+")


def to_pascal_case(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = _CAMEL_BOUNDARY_RE.sub("-", text)
    text = text.replace("_", "-")
    text = _NON_ALNUM_RE.sub("-", text)
    text = re.sub(r"-{2,}", "-", text)
    parts = [part.lower() for part in text.strip("-").split("-") if part]
    if not parts:
        return ""
    return "".join(part[:1].upper() + part[1:] for part in parts)


__all__ = ["to_pascal_case"]
