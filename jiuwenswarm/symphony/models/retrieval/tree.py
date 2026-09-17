from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RetrieverItem:
    item_id: str
    payload: str
    label: str = ""
    description: str = ""


@dataclass(frozen=True)
class RetrieverChoice:
    choice_id: str
    payload: str
    description: str = ""


@dataclass(frozen=True)
class RetrieverNode:
    node_id: str
    label: str
    description: str = ""
    children: tuple["RetrieverNode", ...] = ()
    items: tuple[RetrieverItem, ...] = ()


__all__ = ["RetrieverChoice", "RetrieverItem", "RetrieverNode"]
