from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Sequence

from ..catalog.records import CatalogRecord
from .items_jsonl import is_passthrough_item_uri


def write_manifest(
    output_dir: Path,
    item_paths: Sequence[str | Path],
    records: Sequence[CatalogRecord],
    *,
    mode: str,
    item_type: str | None = None,
) -> None:
    manifest = {
        "mode": mode,
        "count": len(records),
        "item_paths": [_serialize_item_path(path) for path in item_paths],
        "worker_ids": [record.worker_id for record in records],
    }
    if item_type:
        manifest["item_type"] = str(item_type)
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def load_manifest(index_dir: Path) -> Dict[str, object]:
    manifest_path = index_dir / "manifest.json"
    if not manifest_path.exists():
        return {}
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _serialize_item_path(path: str | Path) -> str:
    raw = str(path).strip()
    if is_passthrough_item_uri(raw):
        return raw
    return str(Path(raw).expanduser().resolve())


__all__ = ["load_manifest", "write_manifest"]
