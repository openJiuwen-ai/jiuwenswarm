from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RetrieverCandidate:
    rank: int
    item_id: str
    payload: str
    branch_path: tuple[str, ...]
    label: str = ""
    description: str = ""


__all__ = ["RetrieverCandidate"]
