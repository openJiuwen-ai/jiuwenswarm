# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Pure helpers for filtering persisted todos by generation."""

from __future__ import annotations

from typing import Any, Sequence


def todo_item_generation_token(item: Any) -> str | None:
    """Extract the generation token from a TodoItem or its persisted dict."""
    if isinstance(item, dict):
        value = item.get("generation_token")
    else:
        value = getattr(item, "generation_token", None)
    return value if isinstance(value, str) and value else None


def filter_todos_by_generation(
    items: Sequence[Any],
    token: str | None,
) -> list[Any]:
    """Drop superseded generations; unstamped legacy entries pass."""
    if not token:
        return list(items)
    return [
        item
        for item in items
        if todo_item_generation_token(item) in (None, token)
    ]
