from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CATALOG_FILENAME = "catalog.jsonl"


def load_catalog_by_worker(index_dir: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    path = index_dir / CATALOG_FILENAME
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = _load_catalog_line(line)
        if item is None:
            continue
        worker_id = str(item.get("worker_id") or "").strip()
        if worker_id:
            out[worker_id] = item
    return out


def _load_catalog_line(line: str) -> dict[str, Any] | None:
    try:
        item = json.loads(line)
    except json.JSONDecodeError:
        return None
    return item if isinstance(item, dict) else None
