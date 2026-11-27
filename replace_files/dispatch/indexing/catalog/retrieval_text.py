from __future__ import annotations

import re
from typing import List


_CAMEL_CASE_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def build_embedding_record_text(
    *,
    name: str,
    description: str = "",
    content: str = "",
) -> str:
    parts: List[str] = []
    clean_name = str(name or "").strip()
    clean_description = str(description or "").strip()
    clean_content = str(content or "").strip()
    if clean_name:
        parts.append(f"Skill: {clean_name}")
    if clean_description:
        parts.append(f"Summary: {clean_description}")
    if clean_content:
        parts.append(f"Content:\n{clean_content}")
    return "\n".join(parts)


__all__ = [
    "build_embedding_record_text",
]