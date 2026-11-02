from __future__ import annotations

import re


_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_ALNUM_RE = re.compile(r"[^A-Za-z0-9]+")


def _split_name_parts(value: str) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    text = _CAMEL_BOUNDARY_RE.sub("-", text)
    text = text.replace("_", "-")
    text = _NON_ALNUM_RE.sub("-", text)
    text = re.sub(r"-{2,}", "-", text)
    return [part.lower() for part in text.strip("-").split("-") if part]


def to_kebab_case(value: str) -> str:
    return "-".join(_split_name_parts(value))


def to_camel_case(value: str) -> str:
    parts = _split_name_parts(value)
    if not parts:
        return ""
    head, *tail = parts
    return head + "".join(part[:1].upper() + part[1:] for part in tail)


def to_pascal_case(value: str) -> str:
    parts = _split_name_parts(value)
    if not parts:
        return ""
    return "".join(part[:1].upper() + part[1:] for part in parts)


def to_camel_path(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parts = [segment.strip() for segment in text.split(".") if segment.strip()]
    return ".".join(to_camel_case(part) for part in parts if part)


def to_pascal_path(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parts = [segment.strip() for segment in text.split(".") if segment.strip()]
    return ".".join(to_pascal_case(part) for part in parts if part)


def normalize_name_key(value: str) -> str:
    return to_kebab_case(value).replace("-", "")


def bounded_edit_distance(left: str, right: str, *, max_distance: int) -> int | None:
    if max_distance < 0:
        return None
    if left == right:
        return 0
    if abs(len(left) - len(right)) > max_distance:
        return None

    previous = list(range(len(right) + 1))
    for i, left_char in enumerate(left, start=1):
        current = [i]
        row_min = current[0]
        for j, right_char in enumerate(right, start=1):
            insertion = current[j - 1] + 1
            deletion = previous[j] + 1
            substitution = previous[j - 1] + (0 if left_char == right_char else 1)
            value = min(insertion, deletion, substitution)
            current.append(value)
            if value < row_min:
                row_min = value
        if row_min > max_distance:
            return None
        previous = current

    distance = previous[-1]
    if distance > max_distance:
        return None
    return distance


def fuzzy_name_distance(left: str, right: str) -> int | None:
    normalized_left = normalize_name_key(left)
    normalized_right = normalize_name_key(right)
    if not normalized_left or not normalized_right:
        return None
    if normalized_left == normalized_right:
        return 0
    max_len = max(len(normalized_left), len(normalized_right))
    allowed_distance = 1 if max_len >= 8 else 0
    return bounded_edit_distance(normalized_left, normalized_right, max_distance=allowed_distance)
