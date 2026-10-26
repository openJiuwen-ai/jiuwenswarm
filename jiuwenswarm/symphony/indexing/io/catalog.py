from __future__ import annotations

import json
from pathlib import Path
from typing import List

from indexing.catalog.records import CatalogRecord
from shared.tags import normalize_tags


def load_catalog_records(path: Path) -> List[CatalogRecord]:
    if not path.exists():
        return []
    records: List[CatalogRecord] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        worker_id = str(payload.get("worker_id") or payload.get("skill_id") or "")
        metadata = dict(payload.get("metadata") or {})
        records.append(
            CatalogRecord(
                worker_id=worker_id,
                cid=str(payload.get("cid") or ""),
                name=str(payload.get("name") or ""),
                description=str(payload.get("description") or ""),
                skill_path=str(payload.get("skill_path") or ""),
                branch_path=tuple(str(item) for item in payload.get("branch_path") or ()),
                category=str(payload.get("category") or ""),
                retrieval_text=str(payload.get("retrieval_text") or ""),
                metadata=metadata,
                tags=normalize_tags(payload.get("tags"), metadata.get("tags")),
            )
        )
    return records


__all__ = ["load_catalog_records"]
