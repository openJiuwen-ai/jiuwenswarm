# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Persistent per-skill ``skill_tool`` call counters."""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class SkillCallCounter:
    """Thread-safe per-skill call counter persisted as JSON."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    def get(self, skill_name: str) -> int:
        name = str(skill_name or "").strip()
        if not name:
            return 0
        with self._lock:
            return int(self._counts.get(name, 0))

    def increment(self, skill_name: str) -> int:
        """Increment *skill_name* by one and persist. Returns the new count."""
        name = str(skill_name or "").strip()
        if not name:
            return 0
        with self._lock:
            value = int(self._counts.get(name, 0)) + 1
            self._counts[name] = value
            self._persist_unlocked()
            return value

    def reset(self, skill_name: str) -> None:
        """Clear the counter for *skill_name* (missing keys are a no-op)."""
        name = str(skill_name or "").strip()
        if not name:
            return
        with self._lock:
            if name not in self._counts:
                return
            self._counts[name] = 0
            self._persist_unlocked()

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {k: int(v) for k, v in self._counts.items()}

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning(
                "[SkillCallCounter] failed to load %s", self._path, exc_info=True
            )
            return
        if not isinstance(raw, dict):
            return
        counts: dict[str, int] = {}
        for key, value in raw.items():
            name = str(key or "").strip()
            if not name:
                continue
            try:
                counts[name] = max(int(value), 0)
            except (TypeError, ValueError):
                continue
        self._counts = counts

    def _persist_unlocked(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload: dict[str, Any] = {
                k: int(v) for k, v in sorted(self._counts.items())
            }
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(tmp, self._path)
        except Exception:
            logger.warning(
                "[SkillCallCounter] failed to persist %s", self._path, exc_info=True
            )
