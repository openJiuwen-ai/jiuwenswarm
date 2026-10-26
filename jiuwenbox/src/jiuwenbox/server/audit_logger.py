# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Structured audit logging in JSONL format.

Each sandbox can get its own log file under ``log_dir``; whether it
actually does depends on the ``filename_strategy`` selected at
construction time:

- ``"disabled"`` (**default**): no log files are written at all and the
  ``log_dir`` is never created on disk. ``log_event*`` becomes a no-op
  (the call still hits the standard Python ``logging`` debug stream so
  developers can wire it into stderr if needed) and ``read_logs*``
  returns empty. This keeps a fresh jiuwenbox install from polluting
  the user's home dir; opt in via ``--save-logs DIR``.
- ``"timestamped"``: ``{sandbox_id}-{YYYYMMDDTHHMMSS}.audit.log``. The
  timestamp is captured the **first** time a given sandbox is logged and
  cached for the rest of the process lifetime, so all events for a single
  sandbox land in a single file. The ``.audit.log`` suffix lets multiple
  components (e.g. ProcessRuntime daemon stdout) share the same target
  directory without filename collisions. This is what ``--save-logs DIR``
  / ``JIUWENBOX_SAVE_LOGS_DIR`` selects.

When persistence is on, writes are routed through a dedicated
single-thread executor so disk I/O does not stall the asyncio event
loop on hot paths such as ``exec_in_sandbox``.
"""

from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from jiuwenbox.logging_config import configure_logging
from jiuwenbox.models.common import AuditEvent, AuditEventType
from jiuwenbox.server.workspace import JIUWENBOX_HOME

configure_logging()
logger = logging.getLogger(__name__)


# ISO 8601 basic format keeps lexicographic order == chronological order,
# which is exactly what we want when ``ls``/``find`` enumerates the dir.
_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%S"

FilenameStrategy = Literal["disabled", "plain", "timestamped"]


def _write_jsonl_line(log_file: Path, line: str) -> None:
    """Append ``line`` (already serialized JSON) to ``log_file``.

    POSIX guarantees writes <= ``PIPE_BUF`` (typically 4 KiB) to ``O_APPEND``
    files are atomic, so we can interleave appends from multiple threads
    without corrupting individual JSONL records.
    """
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(line + "\n")


class AuditLogger:
    """Append-only JSONL audit logger, one file per sandbox."""

    def __init__(
        self,
        log_dir: Path | None = None,
        *,
        filename_strategy: FilenameStrategy = "disabled",
    ) -> None:
        # ``log_dir`` is still recorded so callers / tests can inspect it,
        # but it is only realised on disk when persistence is actually on.
        # That avoids a ``mkdir ~/.jiuwenbox/logs`` side effect on first
        # import for users who never opt into ``--save-logs``.
        self.log_dir = log_dir or JIUWENBOX_HOME / "logs"
        self.filename_strategy: FilenameStrategy = filename_strategy
        if self.filename_strategy != "disabled":
            self.log_dir.mkdir(parents=True, exist_ok=True)
        # A single worker keeps writes ordered without forcing the event loop
        # to wait on disk I/O. ``thread_name_prefix`` aids in debugging.
        # We construct it even when disabled so ``flush`` etc. behave
        # uniformly (the executor is cheap to keep idle).
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="jiuwenbox-audit",
        )
        self._lock = threading.Lock()
        # Per-sandbox path cache. ``timestamped`` strategy needs it so all
        # events for one sandbox stay in a single file (timestamp is fixed
        # at first sighting). Other strategies leave it empty.
        self._sandbox_files: dict[str, Path] = {}

    def _resolve_log_file(self, sandbox_id: str) -> Path:
        """Return the (possibly cached) log file path for ``sandbox_id``.

        Thread-safe: callers from ``log_event`` and ``read_logs`` may race.
        """
        with self._lock:
            cached = self._sandbox_files.get(sandbox_id)
            if cached is not None:
                return cached

            if self.filename_strategy == "timestamped":
                # If a previous process left a file for this id (rare —
                # SandboxManager wipes registry on boot — but possible if
                # an operator pre-populates the dir or restarts mid-session),
                # reuse the most recent one rather than scattering events
                # across multiple files for one logical sandbox.
                existing = sorted(
                    self.log_dir.glob(f"{sandbox_id}-*.audit.log"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                if existing:
                    self._sandbox_files[sandbox_id] = existing[0]
                    return existing[0]
                # UTC keeps filenames globally sortable across hosts and
                # avoids ambiguity when operators correlate logs collected
                # from differently-tz'd machines. The format itself drops
                # tz info, but the underlying instant is unambiguous.
                ts = datetime.now(timezone.utc).strftime(_TIMESTAMP_FORMAT)
                path = self.log_dir / f"{sandbox_id}-{ts}.audit.log"
            else:
                path = self.log_dir / f"{sandbox_id}.log"

            self._sandbox_files[sandbox_id] = path
            return path

    def _serialize_event(self, event: AuditEvent) -> tuple[Path, str]:
        log_file = self._resolve_log_file(event.sandbox_id)
        line = json.dumps(event.model_dump(mode="json"), ensure_ascii=False)
        return log_file, line

    def log_event(self, event: AuditEvent) -> None:
        """Schedule a non-blocking append of ``event`` to its sandbox log.

        The serialization happens synchronously on the caller's thread (it is
        cheap for the small audit payloads jiuwenbox produces) but the file
        write is dispatched to a dedicated background thread so callers in
        the asyncio event loop are not blocked on disk I/O.

        ``disabled`` strategy: drops the event. We still hit the standard
        Python ``logging`` debug stream so anyone wiring a stderr handler
        can observe events without needing the on-disk JSONL.
        """
        if self.filename_strategy == "disabled":
            logger.debug(
                "Audit (disabled, dropped): %s %s",
                event.event_type.value, event.sandbox_id,
            )
            return
        log_file, line = self._serialize_event(event)
        with self._lock:
            self._executor.submit(_write_jsonl_line, log_file, line)
        logger.debug("Audit: %s %s", event.event_type.value, event.sandbox_id)

    def log_event_sync(self, event: AuditEvent) -> None:
        """Synchronous append used by tests and shutdown paths."""
        if self.filename_strategy == "disabled":
            logger.debug(
                "Audit (disabled, sync, dropped): %s %s",
                event.event_type.value, event.sandbox_id,
            )
            return
        log_file, line = self._serialize_event(event)
        _write_jsonl_line(log_file, line)
        logger.debug("Audit (sync): %s %s", event.event_type.value, event.sandbox_id)

    def log(
        self,
        event_type: AuditEventType,
        sandbox_id: str,
        **details: object,
    ) -> None:
        """Convenience helper to create and log an event."""
        event = AuditEvent(
            event_type=event_type,
            sandbox_id=sandbox_id,
            details=details,
        )
        self.log_event(event)

    def flush(self, timeout: float | None = None) -> None:
        """Wait for queued audit writes to drain, primarily for tests."""
        with self._lock:
            executor = self._executor
        # Use an empty submit() and wait so any prior writes complete.
        future = executor.submit(lambda: None)
        future.result(timeout=timeout)

    def read_logs(self, sandbox_id: str) -> list[AuditEvent]:
        """Read all audit events for a sandbox.

        ``disabled`` strategy returns ``[]`` because nothing was ever
        persisted; the ``/api/v1/sandboxes/{id}/logs`` endpoint therefore
        also returns empty unless ``--save-logs DIR`` was set at boot.
        """
        if self.filename_strategy == "disabled":
            return []
        self.flush()
        log_file = self._resolve_log_file(sandbox_id)
        if not log_file.exists():
            return []
        events: list[AuditEvent] = []
        for line in log_file.read_text().splitlines():
            if line.strip():
                events.append(AuditEvent.model_validate_json(line))
        return events

    def read_logs_raw(self, sandbox_id: str) -> str:
        """Read raw log text for a sandbox.

        ``disabled`` strategy returns ``""`` (see ``read_logs``).
        """
        if self.filename_strategy == "disabled":
            return ""
        self.flush()
        log_file = self._resolve_log_file(sandbox_id)
        if not log_file.exists():
            return ""
        return log_file.read_text()

    def delete_logs(self, sandbox_id: str) -> None:
        """Delete logs for a sandbox.

        ``disabled`` strategy is a no-op (there is nothing on disk).
        ``timestamped`` strategy may have multiple historical files for a
        re-used sandbox id (extremely unlikely but possible across process
        restarts that pre-populated the dir); wipe all of them so callers
        get a clean ``read_logs`` result afterwards. The cache is cleared
        regardless so the next event allocates a fresh file.
        """
        if self.filename_strategy == "disabled":
            return
        self.flush()
        with self._lock:
            self._sandbox_files.pop(sandbox_id, None)
        if self.filename_strategy == "timestamped":
            for f in self.log_dir.glob(f"{sandbox_id}-*.audit.log"):
                f.unlink(missing_ok=True)
        else:
            (self.log_dir / f"{sandbox_id}.log").unlink(missing_ok=True)
