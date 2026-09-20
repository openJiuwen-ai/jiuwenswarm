"""Preserve native Jiuwen file resources across the duplex event bridge."""
from typing import Any


def normalize_file_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    files = []
    for item in value:
        if not isinstance(item, dict):
            continue
        strings = {
            key: item[key].strip() if isinstance(item.get(key), str) else ""
            for key in ("name", "mime_type", "download_url", "download_token", "path")
        }
        if not strings["name"] or not any(strings[key] for key in ("download_url", "download_token", "path")):
            continue
        size = item.get("size")
        files.append({
            **strings,
            "size": size if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else 0,
        })
    return files
