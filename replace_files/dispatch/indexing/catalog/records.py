from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


@dataclass(frozen=True)
class CatalogRecord:
    skill_id: str
    worker_id: str
    cid: str
    name: str
    description: str
    skill_path: str
    branch_path: tuple[str, ...]
    category: str
    retrieval_text: str
    metadata: Dict[str, object]


__all__ = ["CatalogRecord"]
