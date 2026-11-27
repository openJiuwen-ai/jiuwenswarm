from __future__ import annotations

import re


def parse_ids(output: str) -> list[str]:
    values: list[str] = []
    for line in str(output or "").splitlines():
        cleaned = re.sub(r"^\s*(?:\d+[\).\s:-]+|[-*]\s+)", "", line.strip())
        if not cleaned:
            continue
        values.append(cleaned.split("|", 1)[0].strip())
    if values:
        return values
    return re.findall(r"[A-Za-z][A-Za-z0-9_./-]*", str(output or ""))


__all__ = ["parse_ids"]
