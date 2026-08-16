# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Generic staged-task lifecycle state for long-running DeepAgent tasks."""

from __future__ import annotations

import re
import copy
import math
from datetime import datetime, timezone
from enum import Enum
from typing import Any, TypedDict

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.common.provenance.artifact import (
    ArtifactProvenance,
    normalize_artifact_refs,
)
from jiuwenswarm.common.utils import logger, mask_sensitive


_STATE_KEY = "jiuwenswarm.staged_task_lifecycle"
_STAGED_TASK_KEY = "staged_task"
class StageStatus(str, Enum):
    """Minimal status vocabulary for a staged task."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


ArtifactRef = ArtifactProvenance


class FailureInfo(TypedDict, total=False):
    type: str
    message: str
    retry_attempt: int


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value).lower()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


def _get_session_id(session: Any) -> str | None:
    if session is None:
        return None
    getter = getattr(session, "get_session_id", None)
    value = getter() if callable(getter) else getattr(session, "session_id", None)
    value = str(value).strip() if value is not None else ""
    return value or None


def _extract_staged_task(ctx: AgentCallbackContext) -> dict[str, Any]:
    """Read staged_task from callback extra or structured run_context."""
    inputs = getattr(ctx, "inputs", None)
    explicit_candidates: list[Any] = []
    direct_candidates: list[Any] = []
    extra = getattr(ctx, "extra", None)
    if isinstance(extra, dict):
        explicit_candidates.append(extra.get(_STAGED_TASK_KEY))
        direct_candidates.append(extra)
    if isinstance(inputs, dict):
        explicit_candidates.append(inputs.get(_STAGED_TASK_KEY))
        run_context = inputs.get("run_context")
        if isinstance(run_context, dict):
            explicit_candidates.append(run_context.get(_STAGED_TASK_KEY))
        direct_candidates.extend(
            (inputs.get("metadata"), run_context, inputs)
        )
    else:
        run_context = getattr(inputs, "run_context", None)
        if isinstance(run_context, dict):
            explicit_candidates.append(run_context.get(_STAGED_TASK_KEY))
            direct_candidates.append(run_context)
        else:
            run_context_extra = getattr(run_context, "extra", None)
            if isinstance(run_context_extra, dict):
                explicit_candidates.append(run_context_extra.get(_STAGED_TASK_KEY))
                direct_candidates.append(run_context_extra)
        direct_candidates.append(getattr(inputs, "metadata", None))

    for candidate in explicit_candidates:
        if isinstance(candidate, dict):
            return copy.deepcopy(candidate)
    for candidate in direct_candidates:
        if isinstance(candidate, dict) and any(
            key in candidate
            for key in ("task_id", "stage_id", "stage_name", "artifact_refs", "checkpoint_ref")
        ):
            return copy.deepcopy(candidate)
    return {}


def _safe_metadata(value: Any) -> dict[str, Any]:
    safe = _json_safe(value) if isinstance(value, dict) else {}
    return safe if isinstance(safe, dict) else {}
def _merge_metadata(existing: Any, incoming: Any) -> dict[str, Any]:
    existing_mapping = existing if isinstance(existing, dict) else {}
    incoming_mapping = incoming if isinstance(incoming, dict) else {}
    return _safe_metadata({**existing_mapping, **incoming_mapping})


def _safe_failure_message(value: Any) -> str:
    """Redact bearer and key values before the shared masking helper runs."""
    text = str(value)
    text = re.sub(
        r"(?i)(authorization\s*:\s*bearer\s+|api[_-]?key\s*[=:]\s*)([^\s,;]+)",
        r"\1******",
        text,
    )
    return mask_sensitive(text)





class StagedTaskLifecycleRail(DeepAgentRail):
    """Observe staged task lifecycle and persist JSON-compatible snapshots."""

    # Runs after higher-priority rails and before default-priority rails.
    priority: int = 55

    async def before_invoke(self, ctx: AgentCallbackContext) -> None:
        session = getattr(ctx, "session", None)
        sid = _get_session_id(session)
        if not sid:
            return
        snapshot = self._load(session, sid)
        staged = _extract_staged_task(ctx)
        explicit_task_id = str(staged.get("task_id") or "").strip()
        existing_task_id = str(snapshot["task"].get("task_id") or "").strip()
        if explicit_task_id and existing_task_id and explicit_task_id != existing_task_id:
            snapshot = self._empty(sid)
        task = snapshot["task"]
        task["task_id"] = explicit_task_id or str(task.get("task_id") or sid)
        task["session_id"] = sid
        task["status"] = StageStatus.RUNNING.value
        task["failure"] = None
        task["metadata"] = _merge_metadata(
            task.get("metadata"), staged.get("metadata")
        )
        snapshot["status"] = StageStatus.RUNNING.value
        self._save(session, snapshot)

    async def before_task_iteration(self, ctx: AgentCallbackContext) -> None:
        session = getattr(ctx, "session", None)
        sid = _get_session_id(session)
        if not sid:
            return
        iteration = self._iteration(ctx)
        staged = _extract_staged_task(ctx)
        snapshot = self._load(session, sid)
        self._ensure_task(snapshot, sid, staged)
        stage_id = str(staged.get("stage_id") or f"iteration-{iteration}")
        stage = self._find_stage(snapshot, stage_id)
        if stage is None:
            stage = {
                "stage_id": stage_id,
                "stage_name": str(staged.get("stage_name") or stage_id),
                "stage_status": StageStatus.RUNNING.value,
                "iteration": iteration,
                "started_at": _now(),
                "finished_at": None,
                "artifact_refs": [],
                "checkpoint_ref": None,
                "failure": None,
                "metadata": {},
            }
            snapshot["stages"].append(stage)
        else:
            stage.update(
                {
                    "stage_name": str(staged.get("stage_name") or stage.get("stage_name") or stage_id),
                    "stage_status": StageStatus.RUNNING.value,
                    "iteration": iteration,
                    "finished_at": None,
                    "failure": None,
                }
            )
            stage.setdefault("started_at", _now())
        if "artifact_refs" in staged:
            stage["artifact_refs"] = normalize_artifact_refs(staged["artifact_refs"])
        if "checkpoint_ref" in staged:
            stage["checkpoint_ref"] = _json_safe(staged["checkpoint_ref"])
        stage["metadata"] = _merge_metadata(
            stage.get("metadata"), staged.get("metadata")
        )
        snapshot["current_stage"] = stage_id
        snapshot["status"] = StageStatus.RUNNING.value
        snapshot["task"]["status"] = StageStatus.RUNNING.value
        self._upsert_iteration(snapshot, iteration, stage_id, stage)
        self._save(session, snapshot)

    async def after_task_iteration(self, ctx: AgentCallbackContext) -> None:
        session = getattr(ctx, "session", None)
        sid = _get_session_id(session)
        if not sid:
            return
        iteration = self._iteration(ctx)
        staged = _extract_staged_task(ctx)
        snapshot = self._load(session, sid)
        self._ensure_task(snapshot, sid, staged)
        stage_id = str(
            staged.get("stage_id")
            or snapshot.get("current_stage")
            or f"iteration-{iteration}"
        )
        stage = self._find_stage(snapshot, stage_id)
        if stage is None:
            await self.before_task_iteration(ctx)
            snapshot = self._load(session, sid)
            stage = self._find_stage(snapshot, stage_id)
        if stage is None:
            return
        failure = self._failure(ctx)
        stage["stage_status"] = (
            StageStatus.FAILED.value if failure else StageStatus.COMPLETED.value
        )
        stage["finished_at"] = _now()
        stage["failure"] = failure
        if "artifact_refs" in staged:
            stage["artifact_refs"] = normalize_artifact_refs(staged["artifact_refs"])
        if "checkpoint_ref" in staged:
            stage["checkpoint_ref"] = _json_safe(staged["checkpoint_ref"])
        stage["metadata"] = _merge_metadata(
            stage.get("metadata"), staged.get("metadata")
        )
        self._upsert_iteration(snapshot, iteration, stage_id, stage)
        if failure:
            snapshot["status"] = StageStatus.FAILED.value
            snapshot["task"]["status"] = StageStatus.FAILED.value
            snapshot["task"]["failure"] = failure
        else:
            snapshot["status"] = StageStatus.RUNNING.value
            snapshot["task"]["status"] = StageStatus.RUNNING.value
        self._save(session, snapshot)

    async def after_invoke(self, ctx: AgentCallbackContext) -> None:
        session = getattr(ctx, "session", None)
        sid = _get_session_id(session)
        if not sid:
            return
        snapshot = self._load(session, sid)
        exception = getattr(ctx, "exception", None)
        if exception is not None:
            snapshot["status"] = StageStatus.FAILED.value
            snapshot["task"]["status"] = StageStatus.FAILED.value
            snapshot["task"]["failure"] = self._failure_from_exception(ctx, exception)
        else:
            snapshot["status"] = StageStatus.COMPLETED.value
            snapshot["task"]["status"] = StageStatus.COMPLETED.value
            snapshot["task"]["failure"] = None
        self._save(session, snapshot)

    def get_snapshot(self, session: Any) -> dict[str, Any]:
        """Return a detached JSON-compatible snapshot."""
        return _json_safe(copy.deepcopy(self._load(session, _get_session_id(session))))

    @staticmethod
    def _empty(sid: str | None) -> dict[str, Any]:
        return {
            "task": {
                "task_id": sid,
                "session_id": sid,
                "status": StageStatus.PENDING.value,
                "failure": None,
                "metadata": {},
            },
            "stages": [],
            "current_stage": None,
            "iterations": [],
            "status": StageStatus.PENDING.value,
        }

    def _load(self, session: Any, sid: str | None) -> dict[str, Any]:
        getter = getattr(session, "get_state", None)
        raw = getter(_STATE_KEY) if callable(getter) else None
        if not isinstance(raw, dict):
            return self._empty(sid)
        snapshot = copy.deepcopy(raw)
        snapshot.setdefault("stages", [])
        snapshot.setdefault("iterations", [])
        snapshot.setdefault("current_stage", None)
        snapshot.setdefault("status", StageStatus.PENDING.value)
        task = snapshot.setdefault("task", {})
        if not isinstance(task, dict):
            snapshot["task"] = task = {}
        task.setdefault("task_id", sid)
        task.setdefault("session_id", sid)
        task.setdefault("status", snapshot["status"])
        task.setdefault("failure", None)
        task.setdefault("metadata", {})
        return snapshot

    @staticmethod
    def _save(session: Any, snapshot: dict[str, Any]) -> None:
        updater = getattr(session, "update_state", None)
        if not callable(updater):
            return
        try:
            updater({_STATE_KEY: _json_safe(snapshot)})
        except Exception as exc:
            logger.warning(
                "[StagedTaskLifecycleRail] session snapshot save failed: %s",
                type(exc).__name__,
            )

    @staticmethod
    def _iteration(ctx: AgentCallbackContext) -> int:
        inputs = getattr(ctx, "inputs", None)
        value = getattr(inputs, "iteration", None)
        if value is None and isinstance(inputs, dict):
            value = inputs.get("iteration")
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _ensure_task(snapshot: dict[str, Any], sid: str, staged: dict[str, Any]) -> None:
        task = snapshot["task"]
        task["task_id"] = str(staged.get("task_id") or task.get("task_id") or sid)
        task["session_id"] = sid

    @staticmethod
    def _find_stage(snapshot: dict[str, Any], stage_id: str) -> dict[str, Any] | None:
        return next(
            (
                stage
                for stage in snapshot.get("stages", [])
                if isinstance(stage, dict) and stage.get("stage_id") == stage_id
            ),
            None,
        )

    @staticmethod
    def _upsert_iteration(
        snapshot: dict[str, Any], iteration: int, stage_id: str, stage: dict[str, Any]
    ) -> None:
        record = {
            "iteration": iteration,
            "stage_id": stage_id,
            "status": stage.get("stage_status"),
            "started_at": stage.get("started_at"),
            "finished_at": stage.get("finished_at"),
            "failure": stage.get("failure"),
        }
        for existing in snapshot["iterations"]:
            if isinstance(existing, dict) and existing.get("iteration") == iteration:
                existing.update(record)
                return
        snapshot["iterations"].append(record)

    @classmethod
    def _failure(cls, ctx: AgentCallbackContext) -> FailureInfo | None:
        exception = getattr(ctx, "exception", None)
        if exception is not None:
            return cls._failure_from_exception(ctx, exception)
        inputs = getattr(ctx, "inputs", None)
        result = getattr(inputs, "result", None)
        if result is None and isinstance(inputs, dict):
            result = inputs.get("result")
        if isinstance(result, dict):
            kind = str(result.get("result_type") or "").lower()
            if kind in {"error", "failed", "failure"} or result.get("error") is not None:
                return {
                    "type": "TaskIterationError",
                    "message": _safe_failure_message(result.get("error") or result.get("message") or kind),
                }
        return None

    @staticmethod
    def _failure_from_exception(ctx: AgentCallbackContext, exception: Exception) -> FailureInfo:
        failure: FailureInfo = {
            "type": type(exception).__name__,
            "message": _safe_failure_message(exception),
        }
        attempt = getattr(ctx, "retry_attempt", 0)
        if isinstance(attempt, int) and attempt > 0:
            failure["retry_attempt"] = attempt
        return failure


__all__ = ["ArtifactRef", "FailureInfo", "StageStatus", "StagedTaskLifecycleRail"]
