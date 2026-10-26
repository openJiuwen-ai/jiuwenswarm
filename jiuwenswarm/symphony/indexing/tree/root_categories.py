from __future__ import annotations

from pathlib import Path
from typing import Sequence

from indexing.io.config_loader import parse_json_or_yaml, read_config_text

RootCategory = str | dict[str, object]
RootCategoryInput = Sequence[RootCategory] | str | Path | None


def resolve_tree_root_categories(value: RootCategoryInput) -> list[RootCategory] | None:
    if value in (None, [], ()):
        return None
    if isinstance(value, (str, Path)):
        return load_tree_root_categories(value)
    return list(value)


def load_tree_root_categories(source: str | Path) -> list[RootCategory]:
    raw_source = str(source or "").strip()
    if not raw_source:
        raise ValueError("tree_root_categories file path is empty")
    payload = parse_json_or_yaml(
        read_config_text(raw_source, description="tree_root_categories"),
        source=raw_source,
    )
    return list(_extract_categories(payload, source=raw_source))


def _extract_categories(payload: object, *, source: str) -> list[RootCategory]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("tree_root_categories", "root_categories"):
            value = payload.get(key)
            if value is None:
                continue
            if not isinstance(value, list):
                raise ValueError(f"{source}: field '{key}' must be a list")
            return value
    raise ValueError(f"{source}: expected a root category list or root category object")


__all__ = ["RootCategory", "RootCategoryInput", "load_tree_root_categories", "resolve_tree_root_categories"]
