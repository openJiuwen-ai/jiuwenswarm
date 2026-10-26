# jiuwenswarm/agentserver/deep_agent/auto_harness/task_store.py
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Task metadata storage for scheduled auto_harness tasks."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .run_log_status import (
    META_EVOLVE_STAGE_ORDER,
    ProgressEnricher,
    STAGE_DISPLAY_NAMES,
    SkippedStageInferer,
    TERMINAL_STATUSES,
    determine_pipeline_status_from_log,
    has_terminal_session_event,
    read_key_events_reverse,
    resolve_latest_task_log_path,
    summarize_progress_from_logs,
    summarize_progress_from_key_events,
)

logger = logging.getLogger(__name__)


def _sync_write_json(path: Path, data: str) -> None:
    """Synchronous JSON file write — called via asyncio.to_thread."""
    path.write_text(data, encoding="utf-8")


class TaskStore:
    """Manages scheduled task metadata and execution logs.

    Storage layout:
        ~/.jiuwenswarm/auto-harness/
        ├── scheduled-tasks.json        # Task index
        └── runs/
            └── sch_abc123/
                ├── exec_001/
                │   └── log.json        # Structured log
                └── latest -> exec_001  # Symlink to latest

    Uses in-memory cache for reads to avoid blocking the asyncio event loop.
    Writes persist to disk via asyncio.to_thread.
    """

    def __init__(self, data_dir: Path):
        self._data_dir = data_dir
        self._tasks_file = data_dir / "scheduled-tasks.json"
        self._runs_dir = data_dir / "runs"
        self._tasks_cache: Optional[dict[str, Any]] = None
        self._save_lock: asyncio.Lock = asyncio.Lock()
        self._skipped_stage_inferers: list[SkippedStageInferer] = []
        self._progress_enrichers: list[ProgressEnricher] = []
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        """Ensure required directories exist."""
        self._runs_dir.mkdir(parents=True, exist_ok=True)

    def _load_tasks(self) -> dict[str, Any]:
        """Load tasks — returns in-memory cache if available, otherwise reads from file."""
        if self._tasks_cache is not None:
            return self._tasks_cache

        if not self._tasks_file.exists():
            result = {"tasks": [], "last_updated": None}
            self._tasks_cache = result
            return result

        try:
            data = json.loads(self._tasks_file.read_text(encoding="utf-8"))
            self._tasks_cache = data
            return data
        except Exception as e:
            logger.warning("[TaskStore] Failed to load tasks file: %s", e)
            result = {"tasks": [], "last_updated": None}
            self._tasks_cache = result
            return result

    async def _save_tasks(self, data: dict[str, Any]) -> None:
        """Save tasks — updates in-memory cache first, then persists to disk via to_thread."""
        data["last_updated"] = datetime.now(timezone.utc).isoformat()
        self._tasks_cache = data

        json_str = json.dumps(data, ensure_ascii=False, indent=2)
        async with self._save_lock:
            await asyncio.to_thread(_sync_write_json, self._tasks_file, json_str)

    async def add_task(self, task: dict[str, Any]) -> None:
        """Add a new scheduled task."""
        data = self._load_tasks()
        data["tasks"].append(task)
        await self._save_tasks(data)
        logger.info("[TaskStore] Added task: %s", task.get("task_id"))

    async def update_task(self, task_id: str, updates: dict[str, Any]) -> None:
        """Update an existing task."""
        data = self._load_tasks()
        for task in data.get("tasks", []):
            if task.get("task_id") == task_id:
                task.update(updates)
                break
        await self._save_tasks(data)

    def get_task(self, task_id: str) -> Optional[dict[str, Any]]:
        """Get task by ID — reads from in-memory cache (zero I/O)."""
        data = self._load_tasks()
        for task in data.get("tasks", []):
            if task.get("task_id") == task_id:
                return task
        return None

    def list_tasks(self) -> list[dict[str, Any]]:
        """List all tasks — reads from in-memory cache (zero I/O)."""
        data = self._load_tasks()
        return data.get("tasks", [])

    def register_run_log_status_extension(
        self,
        *,
        skipped_stage_inferer: SkippedStageInferer | None = None,
        progress_enricher: ProgressEnricher | None = None,
    ) -> None:
        """Register optional run-log status extensions for specialized capabilities."""
        if skipped_stage_inferer is not None and skipped_stage_inferer not in self._skipped_stage_inferers:
            self._skipped_stage_inferers.append(skipped_stage_inferer)
        if progress_enricher is not None and progress_enricher not in self._progress_enrichers:
            self._progress_enrichers.append(progress_enricher)

    def summarize_progress_from_logs(self, logs: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
        """Summarize stage progress from structured harness logs."""
        if logs is None:
            logs = []
        return summarize_progress_from_logs(
            logs,
            skipped_stage_inferers=self._skipped_stage_inferers,
            progress_enrichers=self._progress_enrichers,
        )

    def determine_pipeline_status_from_log(self, log_path: Path) -> dict[str, Any]:
        """Determine pipeline status with registered run-log extensions."""
        return determine_pipeline_status_from_log(
            log_path,
            skipped_stage_inferers=self._skipped_stage_inferers,
        )

    async def summarize_task_progress(self, task: dict[str, Any]) -> dict[str, Any]:
        """Read the latest task log and return a compact progress summary.

        对所有任务（含终态）都读取日志获取阶段数据。
        终态任务读取完整日志（以运行 enrichers 提取 PR URL 等），
        运行中任务反向读取关键事件以提升性能。
        """
        log_path = resolve_latest_task_log_path(task, self._runs_dir)
        if not log_path:
            return {
                "summary": "暂无执行日志",
                "stages": [
                    {
                        "stage": stage,
                        "name": STAGE_DISPLAY_NAMES.get(stage, stage),
                        "status": "pending",
                        "messages": [],
                    }
                    for stage in META_EVOLVE_STAGE_ORDER
                ],
                "completed_stages": [],
                "current_stage": "",
                "failed_stage": "",
            }

        task_status = str(task.get("status") or "")
        if task_status in TERMINAL_STATUSES:
            # 终态任务：读取完整日志以运行 enrichers（如提取 PR URL）
            logs = await asyncio.to_thread(self.read_log, log_path, 0, -1)
            progress = self.summarize_progress_from_logs(logs)
        else:
            # 运行中任务：反向读取关键事件
            key_events = await asyncio.to_thread(read_key_events_reverse, log_path, 20)
            if key_events:
                progress = summarize_progress_from_key_events(key_events)
            else:
                # 关键事件为空，读取完整日志
                logs = await asyncio.to_thread(self.read_log, log_path, 0, -1)
                progress = self.summarize_progress_from_logs(logs)

        progress["log_path"] = str(log_path)
        return progress

    async def enrich_task_with_progress(self, task: dict[str, Any]) -> dict[str, Any]:
        """Return a shallow task copy with latest progress attached."""
        enriched = dict(task)
        enriched["progress"] = await self.summarize_task_progress(task)
        return enriched

    async def enrich_tasks_with_progress(self, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Attach progress summaries to a list of tasks."""
        return [await self.enrich_task_with_progress(task) for task in tasks]

    def list_pending_tasks(self) -> list[dict[str, Any]]:
        """List tasks with status 'pending' that are due for execution."""
        data = self._load_tasks()
        now = datetime.now(timezone.utc)
        pending = []
        for task in data.get("tasks", []):
            if task.get("status") != "pending":
                continue
            next_run_str = task.get("next_run_time")
            if not next_run_str:
                continue
            try:
                next_run = datetime.fromisoformat(next_run_str)
                if now >= next_run:
                    pending.append(task)
            except ValueError:
                logger.warning(
                    "[TaskStore] Invalid next_run_time format for task %s: %s",
                    task.get("task_id"), next_run_str
                )
                continue
        return pending

    async def delete_task(self, task_id: str) -> bool:
        """Delete a task and its log files.

        Args:
            task_id: Task identifier

        Returns:
            True if task was deleted, False if not found
        """
        data = self._load_tasks()
        tasks = data.get("tasks", [])

        task_found = False
        new_tasks = []
        for task in tasks:
            if task.get("task_id") == task_id:
                task_found = True
            else:
                new_tasks.append(task)

        if not task_found:
            return False

        data["tasks"] = new_tasks
        await self._save_tasks(data)

        # Remove log directory (in thread to avoid blocking event loop)
        run_dir = self._runs_dir / task_id
        if run_dir.exists():
            try:
                await asyncio.to_thread(shutil.rmtree, run_dir)
                logger.info("[TaskStore] Removed log directory for task: %s", task_id)
            except Exception as e:
                logger.warning("[TaskStore] Failed to remove log directory: %s", e)

        logger.info("[TaskStore] Deleted task: %s", task_id)
        return True

    async def add_execution_record(self, task_id: str, record: dict[str, Any]) -> None:
        """Add execution record to task history."""
        data = self._load_tasks()
        for task in data.get("tasks", []):
            if task.get("task_id") == task_id:
                history = task.get("execution_history", [])
                history.append(record)
                task["execution_history"] = history
                break
        await self._save_tasks(data)

    def get_log_path(self, task_id: str, execution_id: str) -> Path:
        """Get path for execution log file."""
        run_dir = self._runs_dir / task_id / execution_id
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir / "log.json"

    @staticmethod
    def write_log(path: Path, chunks: list[dict[str, Any]]) -> None:
        """Write structured log chunks to file (JSON Lines format)."""
        with path.open("w", encoding="utf-8") as f:
            for chunk in chunks:
                f.write(json.dumps(chunk, ensure_ascii=False) + "\n")

    @staticmethod
    def read_log(path: Path, offset: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        """Read log file (JSON Lines format).

        Supports both JSON Lines format (one JSON object per line) and
        legacy array format for backwards compatibility.

        Args:
            path: Log file path
            offset: Skip this many valid JSON entries
            limit: Maximum number of valid JSON entries to return (default 500, -1 = read all)
        """
        if not path.exists():
            return []

        try:
            logs = []
            valid_count = 0
            read_all = limit <= 0
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        valid_count += 1
                        if valid_count <= offset:
                            continue
                        if read_all or len(logs) < limit:
                            logs.append(entry)
                        else:
                            break
                    except json.JSONDecodeError:
                        pass
            return logs
        except Exception as e:
            logger.warning("[TaskStore] Failed to read log as JSON Lines: %s, trying legacy format", e)

        # Legacy array format fallback (only if JSON Lines failed)
        try:
            content = path.read_text(encoding="utf-8").strip()
            if not content:
                return []
            logs = json.loads(content)
            if isinstance(logs, list):
                return logs[offset:offset + limit]
        except json.JSONDecodeError:
            pass

        return []

    @staticmethod
    def get_log_line_count(path: Path) -> int:
        """Get number of valid JSON entries in log file for streaming offset tracking."""
        if not path.exists():
            return 0
        try:
            count = 0
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        json.loads(line)
                        count += 1
                    except json.JSONDecodeError:
                        pass
            return count
        except Exception as e:
            logger.warning("[TaskStore] Failed to count log lines: %s", e)
            return 0

    async def get_logs(
        self,
        task_id: str,
        log_type: str,
        history_index: int = -1,
        offset: int = 0,
        limit: int = 500
    ) -> dict[str, Any]:
        """Get logs for a task — file reads are done via asyncio.to_thread.

        Args:
            task_id: Task identifier
            log_type: "current" or "history"
            history_index: 0=latest completed, 1=second latest, etc.
            offset: Start reading from this line index (for streaming, default 0)
            limit: Maximum number of lines to return (default 500)

        Returns:
            Dict with logs content and metadata
        """
        task = self.get_task(task_id)
        if not task:
            return {"error": "任务不存在", "task_id": task_id}

        if log_type == "current":
            current_exec_id = task.get("current_execution_id")
            if not current_exec_id:
                return {"error": "当前无正在执行的日志", "task_id": task_id}

            log_path = self._runs_dir / task_id / current_exec_id / "log.json"
            logs = await asyncio.to_thread(self.read_log, log_path, offset, limit)
            total_lines = await asyncio.to_thread(self.get_log_line_count, log_path)
            return {
                "logs": logs,
                "execution_id": current_exec_id,
                "type": "current",
                "total_lines": total_lines,
                "is_running": task.get("status") == "running",
                "has_more": offset + len(logs) < total_lines,
            }

        elif log_type == "history":
            history = task.get("execution_history", [])
            if not history:
                return {"error": "无历史执行记录", "task_id": task_id}

            if history_index < 0 or history_index >= len(history):
                return {"error": f"历史记录索引超出范围 (0-{len(history)-1})", "task_id": task_id}

            sorted_history = sorted(
                history,
                key=lambda r: r.get("completed_at", ""),
                reverse=True
            )

            record = sorted_history[history_index]
            log_path_str = record.get("log_path", "")
            if not log_path_str:
                return {"error": "日志路径为空", "record": record}

            log_path = Path(log_path_str)
            if not log_path.exists():
                log_path = self._runs_dir / task_id / record.get("execution_id", "") / "log.json"

            logs = await asyncio.to_thread(self.read_log, log_path, offset, limit)
            total_lines = await asyncio.to_thread(self.get_log_line_count, log_path)
            if logs:
                return {
                    "logs": logs,
                    "execution_id": record.get("execution_id"),
                    "type": "history",
                    "completed_at": record.get("completed_at"),
                    "status": record.get("status"),
                    "total_lines": total_lines,
                    "has_more": offset + len(logs) < total_lines,
                }
            return {"error": "日志文件不存在或为空", "record": record}

        return {"error": f"未知的 log_type: {log_type}"}

    def has_legacy_completed_tasks(self) -> bool:
        """Check if any task may need log-based status reconciliation."""
        data = self._load_tasks()
        return any(t.get("status") in {"completed", "running"} for t in data.get("tasks", []))

    async def reconcile_task_statuses(self) -> int:
        """Re-check task logs and fix stale status values."""
        data = self._load_tasks()
        corrected = 0

        for task in data.get("tasks", []):
            task_id = task.get("task_id")
            old_status = task.get("status")

            if old_status not in ("completed", "success", "failed", "running"):
                continue

            history = task.get("execution_history", [])
            latest = history[-1] if history else None
            current_execution_id = str(task.get("current_execution_id") or "")
            log_path = resolve_latest_task_log_path(task, self._runs_dir)
            if log_path is None:
                continue

            if old_status == "running" and not has_terminal_session_event(log_path):
                continue

            result = self.determine_pipeline_status_from_log(log_path)
            new_status = "failed" if result["failed"] else "success"

            if new_status != old_status:
                task["status"] = new_status
                task["current_execution_id"] = None
                if latest is None or latest.get("execution_id") != current_execution_id:
                    latest = {
                        "execution_id": current_execution_id,
                        "started_at": task.get("created_at"),
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                        "log_path": str(log_path),
                    }
                    history.append(latest)
                    task["execution_history"] = history
                latest["status"] = new_status
                if result["error"]:
                    latest["error"] = result["error"]
                corrected += 1
                logger.info(
                    "[TaskStore] Reconciled task %s: %s -> %s",
                    task_id, old_status, new_status,
                )

        if corrected > 0:
            await self._save_tasks(data)

        return corrected
