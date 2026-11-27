from __future__ import annotations

from pathlib import Path

from .base import BaseScanner, ScannedItem
from .plugin import PluginScanner
from .skill import SkillScanner


ScannerType = type[BaseScanner]


def normalize_item_type(item_type: str | None) -> str:
    normalized = str(item_type or "skill").strip().lower()
    if normalized not in {"skill", "plugin"}:
        raise ValueError("item_type must be one of: skill, plugin")
    return normalized


def get_scanner_class(item_type: str | None) -> ScannerType:
    normalized = normalize_item_type(item_type)
    return PluginScanner if normalized == "plugin" else SkillScanner


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
    "PluginScanner",
    "ScannedItem",
    "SkillScanner",
    "create_scanner",
    "get_scanner_class",
    "normalize_item_type",
]
