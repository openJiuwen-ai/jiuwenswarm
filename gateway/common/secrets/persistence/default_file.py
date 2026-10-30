"""Default logical-key storage backend (Phase 1: JSON file)."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


class DefaultFileStorageBackend:
    def __init__(self, path: Path) -> None:
        self._path = path

    def read(self, logical_key: str) -> str:
        data = self._load()
        value = data.get(logical_key)
        if value is None:
            return ""
        return str(value)

    def write(self, logical_key: str, raw: str) -> None:
        data = self._load()
        if raw == "":
            data.pop(logical_key, None)
        else:
            data[logical_key] = raw
        self._save(data)

    def delete(self, logical_key: str) -> None:
        data = self._load()
        data.pop(logical_key, None)
        self._save(data)

    def _load(self) -> dict:
        if not self._path.is_file():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to load secrets store %s: %s", self._path, exc)
            return {}

    def _save(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, self._path)
