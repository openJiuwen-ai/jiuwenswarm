"""Durable workspace recovery for interrupted RSI AgentServer tasks."""

from __future__ import annotations

import logging
from typing import Any

from jiuwenswarm.agents.harness.common.rsi.models import TaskStatus

logger = logging.getLogger(__name__)

_ACTIVE_STATES = frozenset({TaskStatus.QUEUED.value, TaskStatus.RUNNING.value})
_PROVIDER_TERMINAL_STATUS = {
    "COMPLETED": TaskStatus.COMPLETED.value,
    "FAILED": TaskStatus.FAILED.value,
    "TERMINATED": TaskStatus.TERMINATED.value,
    "CANCELLED": TaskStatus.TERMINATED.value,
    "CANCELED": TaskStatus.TERMINATED.value,
}


class RsiWorkspaceRecovery:
    """Reconcile persisted task lifecycle after the in-memory worker is lost."""

    def __init__(self, store: Any, projector: Any, adapter_resolver: Any) -> None:
        self.store = store
        self.projector = projector
        self.adapter_resolver = adapter_resolver

    def recover(self) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "scanned": 0,
            "recovered": 0,
            "paused": 0,
            "completed": 0,
            "failed": 0,
            "terminated": 0,
            "errors": [],
        }
        for task in self.store.list():
            if task.status not in _ACTIVE_STATES:
                continue
            summary["scanned"] += 1
            try:
                self.projector.load_from_disk(task.task_id)
                self.projector.register_root(task.task_id)
                target, cause = self._target_status(task)
                self.store.update_status(task.task_id, [task.status], target, cause=cause)
            except Exception as exc:  # noqa: BLE001 - isolate one corrupt task at startup
                logger.exception("[RSI] workspace recovery failed: task=%s", task.task_id)
                summary["errors"].append({"task_id": task.task_id, "error": str(exc)})
                continue
            summary["recovered"] += 1
            summary[target.lower()] += 1
            logger.info(
                "[RSI] workspace task recovered: task=%s from=%s to=%s cause=%s",
                task.task_id,
                task.status,
                target,
                cause,
            )
        logger.info(
            "[RSI] workspace recovery complete: scanned=%s recovered=%s errors=%s",
            summary["scanned"],
            summary["recovered"],
            len(summary["errors"]),
        )
        return summary

    def _target_status(self, task: Any) -> tuple[str, str]:
        if task.status == TaskStatus.QUEUED.value:
            return TaskStatus.PAUSED.value, "agentserver_restart.queue_lost"

        adapter = self.adapter_resolver(task.scenario, task.artifact_type)
        provider_status = _read_provider_status(adapter, task.task_id)
        terminal = _PROVIDER_TERMINAL_STATUS.get(provider_status)
        if terminal is not None:
            return terminal, f"provider_snapshot.{terminal.lower()}"
        return TaskStatus.PAUSED.value, "agentserver_restart.execution_detached"


def _read_provider_status(adapter: Any, task_id: str) -> str:
    reader = getattr(adapter, "read_state", None) if adapter is not None else None
    if not callable(reader):
        return ""
    try:
        state = reader(task_id)
    except Exception as exc:  # noqa: BLE001 - provider recovery is best effort
        logger.warning("[RSI] provider state unavailable during recovery: task=%s error=%s", task_id, exc)
        return ""
    if state is None:
        return ""
    if isinstance(state, dict):
        error_code = str(state.get("error_code") or "").upper()
        status = state.get("status")
    else:
        error_code = str(getattr(state, "error_code", "") or "").upper()
        status = getattr(state, "status", None)
    if error_code == "TASK_NOT_FOUND":
        return ""
    return str(status or "").strip().upper()
