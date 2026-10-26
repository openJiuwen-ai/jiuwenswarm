from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, MutableMapping, Sequence

from models.retrieval import RetrieverItem, RetrieverNode


def choices_cache_key(choices: Sequence[object]) -> str:
    payload = [
        {
            "choice_id": str(getattr(choice, "choice_id", "") or ""),
            "payload": str(getattr(choice, "payload", "") or ""),
            "description": str(getattr(choice, "description", "") or ""),
        }
        for choice in choices
    ]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def build_progressive_root(
    choices: Sequence[object], *, cache: MutableMapping[str, RetrieverNode]
) -> RetrieverNode | None:
    cache_key = choices_cache_key(choices)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    class _NodeBuilder:
        __slots__ = ("node_id", "label", "children", "items")

        def __init__(self, *, node_id: str, label: str) -> None:
            self.node_id = node_id
            self.label = label
            self.children: Dict[str, "_NodeBuilder"] = {}
            self.items: list[RetrieverItem] = []

    root = _NodeBuilder(node_id="ROOT", label="ROOT")
    hierarchical = False
    for choice in choices:
        choice_id = str(getattr(choice, "choice_id", "") or "").strip()
        payload = str(getattr(choice, "payload", "") or "").strip()
        description = str(getattr(choice, "description", "") or "").strip()
        if not choice_id or not payload:
            continue
        parts = [part.strip() for part in payload.split(".") if part.strip()]
        item = RetrieverItem(
            item_id=choice_id,
            payload=payload,
            label=build_progressive_item_label(choice_id=choice_id, payload=payload),
            description=description,
        )
        if len(parts) <= 1:
            root.items.append(item)
            continue
        hierarchical = True
        current = root
        for depth, part in enumerate(parts[:-1], start=1):
            branch_id = ".".join(parts[:depth])
            child = current.children.get(branch_id)
            if child is None:
                child = _NodeBuilder(node_id=branch_id, label=part)
                current.children[branch_id] = child
            current = child
        current.items.append(item)

    if not hierarchical and not root.items:
        return None
    frozen = freeze_progressive_root(root)
    cache[cache_key] = frozen
    return frozen


def freeze_progressive_root(builder: Any) -> RetrieverNode:
    children = [
        freeze_progressive_root(child) for _node_id, child in sorted(builder.children.items(), key=lambda item: item[0])
    ]
    items = sorted(builder.items, key=lambda item: (str(item.label or item.item_id).lower(), str(item.item_id).lower()))
    return RetrieverNode(
        node_id=str(builder.node_id),
        label=str(builder.label),
        description=build_progressive_branch_description(
            label=str(builder.label or builder.node_id), children=children, items=items
        ),
        children=tuple(children),
        items=tuple(items),
    )


def build_progressive_item_label(*, choice_id: str, payload: str) -> str:
    display_name = str(choice_id or "").strip()
    if display_name:
        return display_name
    leaf_term = payload.split(".")[-1].strip() if payload else ""
    return leaf_term or payload


def build_progressive_branch_description(
    *, label: str, children: Sequence[RetrieverNode], items: Sequence[RetrieverItem]
) -> str:
    return ""


__all__ = [
    "build_progressive_branch_description",
    "build_progressive_item_label",
    "build_progressive_root",
    "choices_cache_key",
    "freeze_progressive_root",
]
