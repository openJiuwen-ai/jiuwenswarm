from __future__ import annotations

from typing import Iterable

from models.retrieval import RetrieverNode
from shared.tags import normalize_tags


def candidate_tags_match(
    candidate_tags: Iterable[str],
    requested_tags: Iterable[str],
) -> bool:
    requested = set(normalize_tags(requested_tags))
    if not requested:
        return True
    candidate = set(normalize_tags(candidate_tags))
    if not candidate:
        return False
    if "all" in candidate:
        return True
    return requested.issubset(candidate)


def filter_retriever_tree(root: RetrieverNode, *, allowed_payloads: set[str]) -> RetrieverNode:
    """Prune disallowed leaves and empty ancestors while retaining the root node."""

    filtered = _filter_node(root, allowed_payloads=allowed_payloads, is_root=True)
    return filtered or RetrieverNode(node_id=root.node_id, label=root.label, description=root.description)


def count_retriever_items(root: RetrieverNode) -> int:
    return len(root.items) + sum(count_retriever_items(child) for child in root.children)


def _filter_node(
    node: RetrieverNode,
    *,
    allowed_payloads: set[str],
    is_root: bool,
) -> RetrieverNode | None:
    items = tuple(item for item in node.items if str(item.payload) in allowed_payloads)
    retained_children: list[RetrieverNode] = []
    for current in node.children:
        child = _filter_node(current, allowed_payloads=allowed_payloads, is_root=False)
        if child is not None:
            retained_children.append(child)
    children = tuple(retained_children)
    if not is_root and not items and not children:
        return None
    return RetrieverNode(
        node_id=node.node_id,
        label=node.label,
        description=node.description,
        children=children,
        items=items,
    )


__all__ = [
    "candidate_tags_match",
    "count_retriever_items",
    "filter_retriever_tree",
]
