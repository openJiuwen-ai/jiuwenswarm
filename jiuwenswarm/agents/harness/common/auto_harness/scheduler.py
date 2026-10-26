# jiuwenswarm/agentserver/deep_agent/auto_harness/scheduler.py
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Scheduler for recurring auto_harness task execution."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from openjiuwen.auto_harness.pipelines import META_EVOLVE_PIPELINE
from openjiuwen.core.foundation.llm import Model

from .run_log_status import has_terminal_session_event

if TYPE_CHECKING:
    from .service import AutoHarnessService
    from .task_store import TaskStore

logger = logging.getLogger(__name__)


def _sync_append_log(log_path: Path, line: str) -> None:
    """Synchronous append+flush for log file — called via asyncio.to_thread."""
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line)
        f.flush()


class Scheduler:
    """Async scheduler that triggers scheduled auto_harness tasks.

    Checks pending tasks every 60 seconds and executes those that are due.
    Uses META_EVOLVE_PIPELINE for TUI channel tasks.
    """

    def __init__(
        self,
        service: "AutoHarnessService",
        task_store: "TaskStore",
    ):
        self._service = service
        self._task_store = task_store
        self._loop_task: Optional[asyncio.Task] = None
        self._running_executions: dict[str, asyncio.Task] = {}
        self._model_cache: dict[str, Model] = {}
        self._default_model: Optional[Model] = None

    def _resolve_model(self, model_name: Optional[str] = None) -> Optional[Model]:
        """Resolve model from jiuwenswarm config (same approach as interface_deep).

        Args:
            model_name: Requested model name, falls back to default if None or not found

        Returns:
            Model instance or None if config cannot be loaded
        """
        # Build model cache if not already done
        if not self._model_cache:
            self._build_model_cache()

        # Resolve by name or use default
        if model_name and model_name in self._model_cache:
            return self._model_cache[model_name]
        return self._default_model

    def _build_model_cache(self) -> None:
        """Build model cache from jiuwenswarm config.yaml (reuse interface_deep logic)."""
        try:
            from jiuwenswarm.common.config import get_config, get_default_models
            from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

            config = get_config()

            # Use the same model building method as interface_deep
            build_model_from_entry = getattr(JiuWenSwarmDeepAdapter, '_build_model_from_entry')

            # Build from models.defaults list
            for entry in get_default_models(config):
                mcc = entry.get("model_client_config") or {}
                model_name = mcc.get("model_name")
                if not model_name:
                    continue
                mco = entry.get("model_config_obj") or {}
                self._model_cache[model_name] = build_model_from_entry(mcc, mco)

            # Fallback to legacy format if needed (same as interface_deep._build_model_cache_legacy)
            if not self._model_cache:
                default_model_config = config.get("models", {}).get("default", {})
                react_config = config.get("react", {})
                mcc = dict(
                    default_model_config.get("model_client_config")
                    or react_config.get("model_client_config")
                    or {}
                )
                model_name = mcc.get("model_name") or react_config.get("model_name") or "gpt-4"
                if "model_name" not in mcc:
                    mcc["model_name"] = model_name
                mco = (
                    default_model_config.get("model_config_obj")
                    or react_config.get("model_config_obj")
                    or {}
                )
                self._model_cache[model_name] = build_model_from_entry(mcc, mco)

            # Set default model (first one)
            if self._model_cache:
                first_name = next(iter(self._model_cache))
                self._default_model = self._model_cache[first_name]
                logger.info(
                    "[Scheduler] Built model cache with %d models, default=%s",
                    len(self._model_cache), first_name
                )

        except Exception as e:
            logger.warning("[Scheduler] Failed to build model cache: %s", e)

    async def start(self) -> None:
        """Start the scheduling loop."""
        if self._loop_task is not None:
            logger.warning("[Scheduler] Already running")
            return

        self._loop_task = asyncio.create_task(self._schedule_loop())
        logger.info("[Scheduler] Started scheduling loop")

    async def stop(self) -> None:
        """Stop the scheduler and cancel running executions."""
        # Cancel all running executions
        for task_id, exec_task in list(self._running_executions.items()):
            exec_task.cancel()
            try:
                await exec_task
            except asyncio.CancelledError:
                pass

        self._running_executions.clear()

        # Cancel the loop
        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None

        logger.info("[Scheduler] Stopped")

    async def cancel_execution(self, task_id: str) -> bool:
        """Cancel a running execution for a task.

        Returns:
            True if execution was cancelled, False if not running
        """
        exec_task = self._running_executions.get(task_id)
        if exec_task is None or exec_task.done():
            return False

        # Get current execution_id to build session_id
        task_data = self._task_store.get_task(task_id)
        execution_id = task_data.get("current_execution_id") if task_data else None
        started_at_str = None
        log_path_str = None

        # Try to get started_at from execution_history (most recent)
        if task_data:
            history = task_data.get("execution_history", [])
            if history:
                # Find the execution record for this execution_id
                for record in reversed(history):
                    if record.get("execution_id") == execution_id:
                        started_at_str = record.get("started_at")
                        log_path_str = record.get("log_path")
                        break

        # Cancel the internal service run first (orchestrator execution)
        if execution_id:
            session_id = f"sched_{task_id}_{execution_id}"
            logger.info("[Scheduler] Cancelling service run for session %s", session_id)
            self._service.cancel_session_run(session_id)

        # Cancel the scheduler-level asyncio.Task
        logger.info("[Scheduler] Cancelling asyncio.Task for task %s", task_id)
        exec_task.cancel()
        try:
            await exec_task
        except asyncio.CancelledError:
            logger.info("[Scheduler] CancelledError caught for task %s", task_id)

        # Remove from running dict (before adding execution record to avoid race)
        self._running_executions.pop(task_id, None)

        # Record execution history if we have execution_id
        # (This ensures history is recorded even if _execute_scheduled_task's finally block didn't run)
        if execution_id and task_data:
            completed_at = datetime.now(timezone.utc)
            logger.info(
                "[Scheduler] Recording execution history for cancelled task %s, execution_id %s",
                task_id, execution_id
            )

            # Build log path if not found
            if not log_path_str:
                log_path = self._task_store.get_log_path(task_id, execution_id)
                log_path_str = str(log_path)

            # Use current time as started_at if not found
            if not started_at_str:
                started_at_str = completed_at.isoformat()

            await self._task_store.add_execution_record(task_id, {
                "execution_id": execution_id,
                "started_at": started_at_str,
                "completed_at": completed_at.isoformat(),
                "status": "cancelled",
                "error": "User cancelled",
                "log_path": log_path_str,
            })

            # Update task status to cancelled
            await self._task_store.update_task(task_id, {
                "status": "cancelled",
                "current_execution_id": None,
            })
            logger.info("[Scheduler] Task %s execution %s marked as cancelled in history", task_id, execution_id)

        logger.info("[Scheduler] Cancelled execution for task: %s", task_id)
        return True

    async def trigger_immediate(self, task_id: str) -> bool:
        """Trigger immediate execution of a pending task.

        Returns:
            True if execution was triggered, False if task not found or already running
        """
        logger.info("[Scheduler] trigger_immediate called for task: %s", task_id)

        if task_id in self._running_executions:
            logger.warning("[Scheduler] Task %s already running", task_id)
            return False

        task = self._task_store.get_task(task_id)
        if not task:
            logger.warning("[Scheduler] Task %s not found in task_store", task_id)
            return False

        if task.get("status") != "pending":
            logger.warning("[Scheduler] Task %s not in pending status: %s", task_id, task.get("status"))
            return False

        # Spawn execution immediately
        logger.info("[Scheduler] Spawning execution task for: %s", task_id)
        exec_task = asyncio.create_task(
            self._execute_scheduled_task(task)
        )
        self._running_executions[task_id] = exec_task
        logger.info("[Scheduler] Triggered immediate execution for task: %s", task_id)
        return True

    async def _schedule_loop(self) -> None:
        """Main scheduling loop - check pending tasks every 60 seconds."""
        while True:
            try:
                pending_tasks = self._task_store.list_pending_tasks()

                for task in pending_tasks:
                    task_id = task.get("task_id")
                    if task_id and task_id not in self._running_executions:
                        # Spawn execution
                        exec_task = asyncio.create_task(
                            self._execute_scheduled_task(task)
                        )
                        self._running_executions[task_id] = exec_task

                await asyncio.sleep(60)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("[Scheduler] Loop error: %s", e)
                await asyncio.sleep(60)

    async def _execute_scheduled_task(self, task: dict[str, Any]) -> None:
        """Execute a single scheduled task run.

        Uses the agent already set on the service (set by request handler before execution).
        """
        task_id = task.get("task_id")
        query = task.get("query")
        interval_hours = task.get("interval_hours", 4)
        model_name = task.get("model_name")
        pipeline = task.get("pipeline")  # Pipeline preference from task

        if not task_id or not query:
            logger.warning("[Scheduler] Invalid task data: %s", task)
            return

        execution_id = f"exec_{uuid.uuid4().hex[:8]}"
        session_id = f"sched_{task_id}_{execution_id}"

        # Update status to running
        await self._task_store.update_task(task_id, {
            "status": "running",
            "current_execution_id": execution_id,
        })

        started_at = datetime.now(timezone.utc)
        log_path = self._task_store.get_log_path(task_id, execution_id)
        final_status = "success"
        error_msg = ""

        try:
            # Agent should already be set on service by the request handler
            # (similar to how _handle_command_compact works)
            logger.info(
                "[Scheduler] Using agent from service for task %s: %s",
                task_id, self._service._agent is not None
            )

            # Build request for execution
            from jiuwenswarm.common.schema.agent import AgentRequest

            # Resolve pipeline preference (use task's pipeline or default to META_EVOLVE_PIPELINE)
            pipeline_preference = pipeline if pipeline else META_EVOLVE_PIPELINE
            params = {
                "mode": "auto_harness",
                "scheduled": True,
                "pipeline_preference": pipeline_preference,
            }
            optimization_task = task.get("optimization_task")
            if isinstance(optimization_task, dict):
                params["optimization_task"] = optimization_task
            repo_url = task.get("repo_url", "")
            if repo_url:
                params["repo_url"] = repo_url

            request = AgentRequest(
                request_id=execution_id,
                channel_id="tui",
                session_id=session_id,
                params=params,
            )

            # Resolve model from jiuwenswarm config (same approach as interface_deep)
            model = self._resolve_model(model_name)
            logger.info(
                "[Scheduler] Resolved model for task %s: %s (requested=%s)",
                task_id, model is not None, model_name
            )

            # Execute via service.run() - service already has agent set
            # Auto-accept interactions: scheduled runs have no interactive channel
            async for chunk in self._service.run(
                request, session_id, execution_id, query=query, model=model, auto_accept=True
            ):
                if chunk.payload:
                    # Skip context compression events - not needed in logs
                    event_type = chunk.payload.get("event_type", "")
                    if event_type in ("context.usage", "context.compression_state"):
                        continue
                    if event_type == "harness.message" and chunk.payload.get("stage"):
                        logger.info(
                            "[Scheduler] Task %s execution %s stage=%s message=%s",
                            task_id,
                            execution_id,
                            chunk.payload.get("stage"),
                            str(chunk.payload.get("content") or "")[:160],
                        )
                    elif event_type == "harness.stage_result":
                        logger.info(
                            "[Scheduler] Task %s execution %s stage=%s status=%s error=%s",
                            task_id,
                            execution_id,
                            chunk.payload.get("stage"),
                            chunk.payload.get("status"),
                            str(chunk.payload.get("error") or "")[:200],
                        )
                    # Append log chunk via thread pool (avoids blocking event loop)
                    line = json.dumps(chunk.payload, ensure_ascii=False) + "\n"
                    await asyncio.to_thread(_sync_append_log, log_path, line)
                    if event_type == "harness.session_finished" and chunk.payload.get("is_terminal") is True:
                        logger.info(
                            "[Scheduler] Task %s execution %s received terminal session event",
                            task_id,
                            execution_id,
                        )
                        break

            logger.info(
                "[Scheduler] Task %s execution %s completed successfully",
                task_id, execution_id
            )

        except asyncio.CancelledError:
            final_status = "cancelled"
            logger.info("[Scheduler] Task %s execution %s cancelled", task_id, execution_id)

        except Exception as e:
            final_status = "failed"
            error_msg = str(e)
            logger.exception("[Scheduler] Task %s execution %s failed: %s", task_id, execution_id, e)

        finally:
            if final_status == "success" and log_path.exists() and not has_terminal_session_event(log_path):
                logger.warning(
                    "[Scheduler] Task %s execution %s ended without terminal session event",
                    task_id,
                    execution_id,
                )
            if final_status == "success" and log_path.exists():
                result = self._task_store.determine_pipeline_status_from_log(log_path)
                if result["failed"]:
                    final_status = "failed"
                    error_msg = result["error"]

            # Record execution
            completed_at = datetime.now(timezone.utc)
            await self._task_store.add_execution_record(task_id, {
                "execution_id": execution_id,
                "started_at": started_at.isoformat(),
                "completed_at": completed_at.isoformat(),
                "status": final_status,
                "error": error_msg,
                "log_path": str(log_path),
            })

            # Update next run time if not cancelled
            if final_status != "cancelled":
                is_one_time = task.get("is_one_time", False)
                if is_one_time:
                    await self._task_store.update_task(task_id, {
                        "status": final_status,
                        "current_execution_id": None,
                    })
                    logger.info("[Scheduler] One-time task %s finished with status: %s", task_id, final_status)
                else:
                    next_run = completed_at + timedelta(hours=interval_hours)
                    await self._task_store.update_task(task_id, {
                        "status": "pending",
                        "current_execution_id": None,
                        "next_run_time": next_run.isoformat(),
                    })
            else:
                await self._task_store.update_task(task_id, {
                    "status": "cancelled",
                    "current_execution_id": None,
                })

            # Remove from running dict
            self._running_executions.pop(task_id, None)
