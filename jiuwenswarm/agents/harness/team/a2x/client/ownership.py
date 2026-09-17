"""Persistent tracker for services registered by this client.

The ownership file is a single JSON document shared across backends, keyed by
``base_url`` so one developer can talk to several registries from the same
host. Every mutation re-reads the file under a cross-platform file lock,
updates the in-memory snapshot, and atomically rewrites (tmp + ``os.replace``);
this keeps concurrent SDK processes (D2) and the async thread pool (D9) from
losing each other's updates.

File format (D11 — schema-versioned):

    {
      "schema_version": 1,
      "data": {
        "<base_url>": { "<dataset>": ["<sid>", ...] }
      }
    }

Legacy files without ``schema_version`` (flat ``{base_url: ...}``) are still
loaded for backward compatibility.

A ``file_path`` of ``None`` activates in-memory-only mode (useful for tests
and disposable clients). ``_save`` failures are downgraded to ``warnings``
(D8): the HTTP call has already succeeded, so raising would make callers
wrongly retry and create duplicates.
"""

from __future__ import annotations

import json
import os
import sys
import warnings
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from typing import Any, Iterator

_SCHEMA_VERSION = 1
_LOCK_SUFFIX = ".lock"

# ── Cross-platform file lock ─────────────────────────────────────────────────
# Uses a sibling ``<file>.lock`` (never ``os.replace``-d) as the lock target so
# the lock survives atomic rewrites of the data file.

_LOCK_TIMEOUT_SEC = 10.0
"""Best-effort upper bound for acquiring the ownership file lock. Matches
``msvcrt.LK_LOCK``'s ~10s retry budget so sync and async platforms behave
similarly (L5)."""


if sys.platform == "win32":
    import msvcrt

    def _acquire(fd: int) -> None:
        # LK_LOCK already retries for ~10 seconds then raises OSError.
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)

    def _release(fd: int) -> None:
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass

else:
    import fcntl
    import time

    def _acquire(fd: int) -> None:
        # Poll with LOCK_NB so a stuck peer cannot block us forever (L5).
        deadline = time.monotonic() + _LOCK_TIMEOUT_SEC
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise OSError("timeout acquiring ownership file lock")
                time.sleep(0.05)

    def _release(fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass


@contextmanager
def _file_lock(data_path: Path) -> Iterator[None]:
    data_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = data_path.with_suffix(data_path.suffix + _LOCK_SUFFIX)
    # ``a+b`` creates the file if missing and is seek-safe on both platforms.
    with open(lock_path, "a+b") as f:
        # Ensure there is at least one byte to lock on Windows.
        if os.fstat(f.fileno()).st_size == 0:
            f.write(b"\x00")
            f.flush()
        f.seek(0)
        _acquire(f.fileno())
        try:
            yield
        finally:
            _release(f.fileno())


# ── Store ────────────────────────────────────────────────────────────────────

class OwnershipStore:
    def __init__(self, file_path: Path | None, base_url: str) -> None:
        self._file_path = file_path
        self._base_url = base_url
        self._data: dict[str, set[str]] = {}
        self._lock = Lock()
        self._load()

    # ── Public queries / mutations ───────────────────────────────────────────

    def contains(self, dataset: str, service_id: str) -> bool:
        with self._lock:
            return service_id in self._data.get(dataset, set())

    def add(self, dataset: str, service_id: str) -> None:
        with self._lock:
            self._data.setdefault(dataset, set()).add(service_id)
            self._save_locked()

    def remove(self, dataset: str, service_id: str) -> None:
        with self._lock:
            bucket = self._data.get(dataset)
            if bucket is None:
                return
            bucket.discard(service_id)
            if not bucket:
                self._data.pop(dataset, None)
            self._save_locked()

    def remove_dataset(self, dataset: str) -> None:
        with self._lock:
            if dataset not in self._data:
                return
            self._data.pop(dataset, None)
            self._save_locked()

    # ── File I/O ─────────────────────────────────────────────────────────────

    def _load(self) -> None:
        if self._file_path is None or not self._file_path.exists():
            return
        try:
            raw = json.loads(self._file_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # Corrupt or unreadable file → start clean (first-run friendly).
            return
        segment = self._extract_segment(raw)
        if not isinstance(segment, dict):
            return
        for dataset, ids in segment.items():
            if isinstance(ids, list):
                self._data[dataset] = {sid for sid in ids if isinstance(sid, str)}

    def _extract_segment(self, raw: Any) -> Any:
        if not isinstance(raw, dict):
            return None
        if raw.get("schema_version") == _SCHEMA_VERSION:
            data = raw.get("data")
            if isinstance(data, dict):
                return data.get(self._base_url)
            return None
        # Legacy flat format: {base_url: {dataset: [...]}}
        return raw.get(self._base_url)

    def _save_locked(self) -> None:
        """Persist under the instance lock. Failures are downgraded to warnings."""
        if self._file_path is None:
            return
        try:
            self._save_to_disk()
        except OSError as exc:
            warnings.warn(
                f"a2x-client: failed to persist ownership to {self._file_path}: {exc}. "
                "In-memory state is correct; a later successful write will catch up.",
                RuntimeWarning,
                stacklevel=3,
            )

    def _save_to_disk(self) -> None:
        if self._file_path is None:
            raise RuntimeError("_save_to_disk called without a file_path")
        path = self._file_path
        with _file_lock(path):
            existing = self._read_existing_locked(path)
            data_section = existing.setdefault("data", {})
            if self._data:
                data_section[self._base_url] = {ds: sorted(ids) for ds, ids in self._data.items()}
            else:
                data_section.pop(self._base_url, None)

            tmp_path = path.with_suffix(path.suffix + ".tmp")
            # fsync before replace so a power loss between write and rename
            # cannot leave an empty/partial file behind (L4).
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(json.dumps(existing, indent=2, ensure_ascii=False))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)

    @staticmethod
    def _read_existing_locked(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {"schema_version": _SCHEMA_VERSION, "data": {}}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"schema_version": _SCHEMA_VERSION, "data": {}}
        if isinstance(raw, dict) and raw.get("schema_version") == _SCHEMA_VERSION:
            if not isinstance(raw.get("data"), dict):
                raw["data"] = {}
            return raw
        # Migrate legacy v0 flat shape into v1 wrapper.
        migrated: dict[str, Any] = {"schema_version": _SCHEMA_VERSION, "data": {}}
        if isinstance(raw, dict):
            for key, value in raw.items():
                if isinstance(value, dict):
                    migrated["data"][key] = value
        return migrated
