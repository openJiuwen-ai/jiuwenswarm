# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Persistent per-skill usage counters.

Thread-safe within one process (``threading.Lock``) and cross-process safe
when multiple workers share the same counter JSON path (portalocker companion
``.lock`` file around each read-modify-write).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import portalocker

logger = logging.getLogger(__name__)

_FILE_LOCK_TIMEOUT_SEC = 5.0


class SkillCallCounter:
    """Thread- and cross-process-safe per-skill call counter persisted as JSON."""

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
            with self._file_lock():
                self._reload_unlocked()
                return int(self._counts.get(name, 0))

    def increment(self, skill_name: str) -> int:
        """Increment *skill_name* by one and persist. Returns the new count."""
        name = str(skill_name or "").strip()
        if not name:
            return 0
        with self._lock:
            with self._file_lock():
                self._reload_unlocked()
                value = int(self._counts.get(name, 0)) + 1
                self._counts[name] = value
                self._persist_unlocked()
                return value

    def add(self, skill_name: str, delta: int) -> int:
        """Add *delta* (>= 0) to *skill_name* and persist. Returns the new count."""
        name = str(skill_name or "").strip()
        amount = max(int(delta), 0)
        if not name:
            return 0
        with self._lock:
            with self._file_lock():
                self._reload_unlocked()
                value = int(self._counts.get(name, 0)) + amount
                if amount:
                    self._counts[name] = value
                    self._persist_unlocked()
                return value

    def reset(self, skill_name: str) -> int:
        """Clear the counter for *skill_name*. Returns the count that was cleared."""
        name = str(skill_name or "").strip()
        if not name:
            return 0
        with self._lock:
            with self._file_lock():
                self._reload_unlocked()
                previous = int(self._counts.get(name, 0))
                if not previous:
                    return 0
                self._counts[name] = 0
                self._persist_unlocked()
                return previous

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            with self._file_lock():
                self._reload_unlocked()
                return {k: int(v) for k, v in self._counts.items()}

    @contextmanager
    def _file_lock(self) -> Iterator[None]:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self._path.with_suffix(self._path.suffix + ".lock")
        with portalocker.Lock(
            str(lock_path), mode="a+", timeout=_FILE_LOCK_TIMEOUT_SEC
        ):
            yield

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            with self._file_lock():
                self._reload_unlocked()
        except Exception:
            logger.warning(
                "[SkillCallCounter] failed to load %s", self._path, exc_info=True
            )

    def _reload_unlocked(self) -> None:
        if not self._path.exists():
            self._counts = {}
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning(
                "[SkillCallCounter] failed to reload %s", self._path, exc_info=True
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
