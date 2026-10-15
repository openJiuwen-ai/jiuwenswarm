"""Thread-safe, run-scoped execution progress and validation event writer."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from io_utils import safe_join, write_json_atomic


MONITOR_PATH = "outputs/execution-monitor.json"
EVENTS_PATH = "outputs/execution-events.jsonl"


class ExecutionMonitor:
    """Persist a live snapshot plus append-only events for concurrent runs."""

    def __init__(self, run_dir: Path, run_ids: list[str], *, max_workers: int) -> None:
        self._run_dir = run_dir
        self._snapshot_path = safe_join(run_dir, MONITOR_PATH)
        self._events_path = safe_join(run_dir, EVENTS_PATH)
        self._lock = threading.RLock()
        self._sequence = 0
        self._max_observed_parallelism = 0
        self._runs: dict[str, dict[str, Any]] = {
            run_id: {
                "status": "QUEUED",
                "attempt": 0,
                "pid": None,
                "started_at_utc": None,
                "updated_at_utc": None,
                "finished_at_utc": None,
                "last_validation": None,
                "error": None,
            }
            for run_id in run_ids
        }
        self._max_workers = max_workers
        self._snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        if self._events_path.exists():
            if self._events_path.is_symlink() or not self._events_path.is_file():
                raise ValueError("execution event path must be a regular file")
            self._events_path.unlink()
        self._write_snapshot_locked()
        self.event(None, "SCHEDULER_STARTED", {"max_workers": max_workers})

    def event(
        self,
        run_record_id: str | None,
        event: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            now = datetime.now(timezone.utc).isoformat()
            self._sequence += 1
            payload = {
                "sequence": self._sequence,
                "timestamp_utc": now,
                "run_record_id": run_record_id,
                "event": event,
                "details": details or {},
            }
            if run_record_id is not None:
                state = self._runs[run_record_id]
                state["updated_at_utc"] = now
                self._apply_event(state, event, payload["details"], now)
            self._append_event_locked(payload)
            self._write_snapshot_locked()

    def finish(self) -> None:
        with self._lock:
            self.event(
                None,
                "SCHEDULER_FINISHED",
                {
                    "completed": sum(
                        item["status"] == "SUCCEEDED" for item in self._runs.values()
                    ),
                    "failed": sum(
                        item["status"] in {"FAILED", "TIMEOUT", "VALIDATION_FAILED"}
                        for item in self._runs.values()
                    ),
                },
            )

    def _apply_event(
        self,
        state: dict[str, Any],
        event: str,
        details: dict[str, Any],
        now: str,
    ) -> None:
        status_map = {
            "RUN_STARTED": "RUNNING",
            "HEARTBEAT": "RUNNING",
            "ATTEMPT_RETRY": "RETRYING",
            "RUN_SUCCEEDED": "SUCCEEDED",
            "RUN_FAILED": "FAILED",
            "RUN_TIMEOUT": "TIMEOUT",
            "VALIDATION_FAILED": "VALIDATION_FAILED",
        }
        if event in status_map:
            state["status"] = status_map[event]
        if event == "RUN_STARTED":
            state["started_at_utc"] = state["started_at_utc"] or now
            state["pid"] = details.get("pid")
            state["attempt"] = details.get("attempt", state["attempt"])
        elif event == "ATTEMPT_RETRY":
            state["attempt"] = details.get("next_attempt", state["attempt"])
            state["error"] = details.get("reason")
        elif event == "HEARTBEAT":
            state["last_validation"] = details.get("validation", "PASS")
            state["pid"] = details.get("pid", state["pid"])
        elif event in {"RUN_SUCCEEDED", "RUN_FAILED", "RUN_TIMEOUT", "VALIDATION_FAILED"}:
            state["finished_at_utc"] = now
            state["pid"] = None
            state["error"] = details.get("error")

        running = sum(
            item["status"] in {"RUNNING", "RETRYING"}
            for item in self._runs.values()
        )
        self._max_observed_parallelism = max(self._max_observed_parallelism, running)

    def _append_event_locked(self, payload: dict[str, Any]) -> None:
        if self._events_path.exists() and self._events_path.is_symlink():
            raise ValueError("execution event log cannot be a symbolic link")
        with self._events_path.open("a", encoding="utf-8", newline="\n") as file:
            file.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
            file.flush()
            os.fsync(file.fileno())

    def _write_snapshot_locked(self) -> None:
        statuses = [item["status"] for item in self._runs.values()]
        snapshot = {
            "schema_version": "1.0.0",
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
            "total": len(self._runs),
            "queued": statuses.count("QUEUED"),
            "running": sum(item in {"RUNNING", "RETRYING"} for item in statuses),
            "succeeded": statuses.count("SUCCEEDED"),
            "failed": sum(
                item in {"FAILED", "TIMEOUT", "VALIDATION_FAILED"}
                for item in statuses
            ),
            "configured_max_workers": self._max_workers,
            "max_observed_parallelism": self._max_observed_parallelism,
            "runs": self._runs,
            "events_path": EVENTS_PATH,
        }
        write_json_atomic(self._snapshot_path, snapshot)
