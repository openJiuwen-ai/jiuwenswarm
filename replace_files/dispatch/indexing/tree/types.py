from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .schema import TreeNode


@dataclass(frozen=True)
class QueuedNode:
    """Pending subtree build work item."""

    node: TreeNode
    skills: list[dict]
    depth: int
    parent_context: Optional[dict]


@dataclass
class ChildGroup:
    """Mutable wrapper for a child node and the skill dicts assigned under it."""

    node: TreeNode
    skills: list[dict]
    configured_children: dict | None = None
