from __future__ import annotations

from pathlib import Path

from .base import BaseScanner, ScannedItem
from .skill import SkillScanner


ScannerType = type[BaseScanner]


def normalize_item_type(item_type: str | None) -> str:
    normalized = str(item_type or "skill").strip().lower()
    if normalized != "skill":
        raise ValueError("item_type must be: skill")
    return normalized


def get_scanner_class(item_type: str | None) -> ScannerType:
    normalize_item_type(item_type)
    return SkillScanner


def create_scanner(
    item_type: str | None,
    items_dir: Path | str,
    *,
    display_items_dir: Path | str | None = None,
) -> BaseScanner:
    scanner_cls = get_scanner_class(item_type)
    return scanner_cls(items_dir, display_items_dir=display_items_dir)


__all__ = [
    "BaseScanner",
    "ScannedItem",
    "SkillScanner",
    "create_scanner",
    "get_scanner_class",
    "normalize_item_type",
]
