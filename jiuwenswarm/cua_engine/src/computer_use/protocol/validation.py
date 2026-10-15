"""Validate public text, paste and key intents before native dispatch."""
from __future__ import annotations

import re

from .primitives import nonempty


def _unicode(value):
    # A pair of explicit surrogate code points is also valid JS Unicode input.
    try:
        value.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "strict")
        return True
    except UnicodeError:
        return False


def validate_text(value):
    if not isinstance(value, str) or re.search(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]", value):
        raise ValueError("typeText expects one line of text; use pressKey for control keys")
    if not _unicode(value):
        raise ValueError("typeText expects valid Unicode")


def validate_paste_text(value):
    if not isinstance(value, str) or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", value):
        raise ValueError("paste expects plain text; only tabs and line breaks are allowed control characters")
    if not _unicode(value):
        raise ValueError("paste expects valid Unicode")


_ORDINARY = [
    "Return", "Tab", "Escape", "BackSpace", "Delete", "space", "Left", "Right", "Up", "Down", "Home", "End", "Page_Up",
    "Page_Down", *[f"F{i}" for i in range(1, 13)]
]


def validate_key(value):
    nonempty(value, "key")
    *parts, key = value.split("+")
    supported_key = key in _ORDINARY or re.fullmatch(r"[a-z0-9]", key) or not parts and key == "super"
    if (not supported_key or len(set(parts)) != len(parts)
            or any(p not in ("super", "ctrl", "alt", "shift") for p in parts)):
        raise ValueError((f"unsupported key or modifiers: {value}. Use a–z, 0–9, {', '.join(_ORDINARY)}; "
                          f"modifiers: super, ctrl, alt, shift (joined with +)"))
