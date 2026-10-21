# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Per-session JSON persistence of diagnosis records.

Layout: ``<sessions_dir>/<session_id>/diagnosis/<diagnosis_id>.json`` plus an
``index.json`` that hands out increasing ordinals. Files are written
atomically (temporary file + ``os.replace``) so a crash never leaves a torn
record. Records are never pruned automatically; users delete them one by one.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from jiuwenswarm.common.protocol_ids import is_valid_session_id
from jiuwenswarm.diagnosis.models import (
    DiagnosisRecord,
    DiagnosisStatus,
    DiagnosisSummary,
    StageStatus,
)

logger = logging.getLogger(__name__)

DIAGNOSIS_DIRECTORY = "diagnosis"
_INDEX_FILE = "index.json"
_READ_ATTEMPTS = 5
_READ_RETRY_SECONDS = 0.01
_REPLACE_ATTEMPTS = 10
_ID_PATTERN = re.compile(r"^dg_[0-9a-f]{16}$")


def new_diagnosis_id() -> str:
    """Return a fresh, filename-safe diagnosis identifier."""
    return f"dg_{uuid.uuid4().hex[:16]}"


def is_valid_diagnosis_id(value: str) -> bool:
    """Return whether ``value`` is a well-formed diagnosis identifier."""
    return bool(_ID_PATTERN.match(value or ""))


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with ``Z`` suffix."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _default_sessions_dir() -> Path:
    from jiuwenswarm.common.utils import get_agent_sessions_dir

    return get_agent_sessions_dir()


class DiagnosisStore:
    """Filesystem store of diagnosis records, keyed by session."""

    def __init__(self, sessions_dir: Path | Callable[[], Path] | None = None) -> None:
        if sessions_dir is None:
            self._sessions_dir: Callable[[], Path] = _default_sessions_dir
        elif callable(sessions_dir):
            self._sessions_dir = sessions_dir
        else:
            fixed = Path(sessions_dir)

            def fixed_sessions_dir() -> Path:
                return fixed

            self._sessions_dir = fixed_sessions_dir
        self._lock = threading.Lock()

    def _directory(self, session_id: str) -> Path:
        if not is_valid_session_id(session_id):
            raise ValueError("invalid session_id")
        root = Path(self._sessions_dir()).resolve()
        directory = (root / session_id / DIAGNOSIS_DIRECTORY).resolve()
        if root not in directory.parents:
            raise ValueError("session_id escapes the sessions directory")
        return directory

    def _record_path(self, session_id: str, diagnosis_id: str) -> Path:
        if not is_valid_diagnosis_id(diagnosis_id):
            raise ValueError("invalid diagnosis_id")
        return self._directory(session_id) / f"{diagnosis_id}.json"

    @staticmethod
    def _atomic_write(path: Path, payload: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            temporary.write_text(payload, encoding="utf-8")
            # On Windows replacing a file that a reader has open fails with
            # ``PermissionError``; the reader releases it within milliseconds.
            for attempt in range(_REPLACE_ATTEMPTS):
                try:
                    os.replace(temporary, path)
                    break
                except PermissionError:
                    if attempt + 1 == _REPLACE_ATTEMPTS:
                        raise
                    time.sleep(_READ_RETRY_SECONDS)
        finally:
            if temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    logger.debug("[diagnosis] temporary file cleanup failed: %s", temporary)

    def allocate_ordinal(self, session_id: str) -> int:
        """Reserve the next per-session ordinal (1-based, never reused)."""
        directory = self._directory(session_id)
        index_path = directory / _INDEX_FILE
        with self._lock:
            next_ordinal = 1
            try:
                payload = json.loads(index_path.read_text(encoding="utf-8"))
                next_ordinal = max(1, int(payload.get("next_ordinal", 1)))
            except (OSError, ValueError, AttributeError):
                existing = [summary.ordinal for summary in self._list_unlocked(session_id)]
                next_ordinal = (max(existing) + 1) if existing else 1
            self._atomic_write(index_path, json.dumps({"next_ordinal": next_ordinal + 1}))
        return next_ordinal

    def save(self, record: DiagnosisRecord) -> None:
        """Persist ``record`` atomically, replacing any earlier version."""
        path = self._record_path(record.session_id, record.diagnosis_id)
        payload = record.model_dump_json(exclude_none=True)
        with self._lock:
            self._atomic_write(path, payload)

    def load(self, session_id: str, diagnosis_id: str) -> DiagnosisRecord | None:
        """Return one record, or ``None`` when missing or unreadable."""
        try:
            path = self._record_path(session_id, diagnosis_id)
        except ValueError:
            return None
        return self._read(path)

    @staticmethod
    def _read(path: Path) -> DiagnosisRecord | None:
        # On Windows a read that overlaps ``os.replace`` of the same file fails
        # with ``PermissionError``; the next attempt sees the new version.
        for attempt in range(_READ_ATTEMPTS):
            try:
                return DiagnosisRecord.model_validate_json(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return None
            except PermissionError:
                if attempt + 1 < _READ_ATTEMPTS:
                    time.sleep(_READ_RETRY_SECONDS)
                    continue
            except (OSError, ValueError):
                pass
            break
        logger.warning("[diagnosis] unreadable record skipped: %s", path)
        return None

    def _list_unlocked(self, session_id: str) -> list[DiagnosisSummary]:
        directory = self._directory(session_id)
        if not directory.is_dir():
            return []
        summaries: list[DiagnosisSummary] = []
        for path in directory.glob("dg_*.json"):
            record = self._read(path)
            if record is not None:
                summaries.append(record.to_summary())
        summaries.sort(key=lambda item: item.ordinal, reverse=True)
        return summaries

    def list_summaries(self, session_id: str) -> list[DiagnosisSummary]:
        """Return summaries of one session, newest ordinal first."""
        return self._list_unlocked(session_id)

    def delete(self, session_id: str, diagnosis_id: str) -> bool:
        """Delete one record; return whether a file was removed."""
        try:
            path = self._record_path(session_id, diagnosis_id)
        except ValueError:
            return False
        with self._lock:
            try:
                path.unlink()
            except FileNotFoundError:
                return False
        return True

    def mark_running_interrupted(self) -> int:
        """Mark every ``running`` record as ``interrupted`` after a restart.

        Returns:
            Number of records that were rewritten.
        """
        root = Path(self._sessions_dir())
        if not root.is_dir():
            return 0
        changed = 0
        for path in root.glob(f"*/{DIAGNOSIS_DIRECTORY}/dg_*.json"):
            record = self._read(path)
            if record is None or record.status is not DiagnosisStatus.RUNNING:
                continue
            record.status = DiagnosisStatus.INTERRUPTED
            record.finished_at = utc_now_iso()
            for stage in record.stages:
                if stage.status is StageStatus.RUNNING:
                    stage.status = StageStatus.FAILED
            with self._lock:
                self._atomic_write(path, record.model_dump_json(exclude_none=True))
            changed += 1
        return changed


__all__ = [
    "DIAGNOSIS_DIRECTORY",
    "DiagnosisStore",
    "is_valid_diagnosis_id",
    "new_diagnosis_id",
    "utc_now_iso",
]
