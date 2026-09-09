"""Persistent storage for distilled skill packs."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PACKS_FILE = "skill_packs.json"
PACKS_SCHEMA_VERSION = "symphony.skill_pack.v1"

_PACKS_LOCK = threading.RLock()


def packs_path(graph_dir: Path) -> Path:
    return Path(graph_dir) / "evolution" / PACKS_FILE


def read_packs(graph_dir: Path) -> dict[str, Any]:
    """Read the current skill packs artifact. Returns empty structure on miss."""
    with _PACKS_LOCK:
        path = packs_path(graph_dir)
        if not path.is_file():
            return {"schema_version": PACKS_SCHEMA_VERSION, "packs": []}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"schema_version": PACKS_SCHEMA_VERSION, "packs": []}
    return payload if isinstance(payload, dict) else {"schema_version": PACKS_SCHEMA_VERSION, "packs": []}


def write_packs(graph_dir: Path, packs: list[dict[str, Any]]) -> None:
    """Atomically write distilled skill packs."""
    payload = {
        "schema_version": PACKS_SCHEMA_VERSION,
        "packs": packs,
        "updated_at": _utc_now(),
    }
    with _PACKS_LOCK:
        path = packs_path(graph_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        tmp_path.replace(path)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
