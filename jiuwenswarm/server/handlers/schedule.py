# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""调度/议题域 handler"""

from __future__ import annotations

import logging
from typing import Any

from jiuwenswarm.agents.harness.common.auto_harness import AutoHarnessService
from jiuwenswarm.common.e2a.wire_codec import encode_agent_response_for_wire
from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.server.context import RequestContext
from jiuwenswarm.server.handlers.session import _coerce_int
from jiuwenswarm.server.handlers._shared import (
    _resolve_model,
    _apply_resolved_mode_to_request,
    resolve_request_project_dir,
)

logger = logging.getLogger(__name__)


def _set_scheduler_agent(ctx, agent: Any) -> None:
    """Pin the facade whose DeepAgent is retained by the scheduler."""
    previous = getattr(ctx.services, "scheduler_agent", None)
    if previous is agent:
        return
    pin = getattr(ctx.services.agent_manager, "pin_agent", None)
    if callable(pin):
        pin(agent)
    ctx.services.scheduler_agent = agent
    if previous is not None:
        unpin = getattr(ctx.services.agent_manager, "unpin_agent", None)
        if callable(unpin):
            unpin(previous)


async def handle_schedule_request(ctx: RequestContext, action: str) -> None:
    """Handle schedule.* requests - schedule task management."""
    request = ctx.request
    logger.info(
        "[AgentServer] schedule.%s request received: request_id=%s channel_id=%s",
        action, request.request_id, request.channel_id,
    )
    try:
        # Lazy initialization: create scheduler service on first request
        if ctx.services.scheduler_service is None:
            logger.info("[AgentServer] Initializing scheduler service on first request")
            ctx.services.scheduler_service = AutoHarnessService(None, agent=None)
            # Start the scheduler loop
            await ctx.services.scheduler_service.start_scheduler()

        params = request.params or {}
        payload: dict[str, Any] = {}

        # For actions that need agent: get agent and set on service (similar to _handle_command_compact)
        needs_agent = action in ("create", "run", "cancel", "delete", "issue_watch_once")
        if needs_agent:
            mode, sub_mode = _apply_resolved_mode_to_request(request)
            agent_mode = "agent" if mode == "auto_harness" else mode
            agent = await ctx.services.agent_manager.get_agent(
                channel_id=request.channel_id or "tui",
                mode=agent_mode,
                project_dir=resolve_request_project_dir(request),
                sub_mode=sub_mode

            )
            if agent is None:
                raise ValueError("Failed to get agent for schedule request")
            # Set agent on service (service will use it for execution)
            await ctx.services.scheduler_service.update_agent_instance(agent)
            _set_scheduler_agent(ctx, agent)
            logger.info("[AgentServer] Set agent for schedule action %s: %s", action, agent is not None)

        if action == "check_config":
            payload = ctx.services.scheduler_service.check_schedule_config()

        elif action == "update_config":
            fields = params.get("fields", {})
            payload = ctx.services.scheduler_service.update_schedule_config(fields)

        elif action == "create":
            query = params.get("query", "")
            interval_hours = params.get("interval_hours", 4)
            run_immediately = params.get("run_immediately", False)
            model_name = params.get("model_name")
            pipeline = params.get("pipeline")  # Pipeline preference
            # Resolve model from jiuwenswarm config
            model = _resolve_model(ctx, model_name)
            payload = await ctx.services.scheduler_service.create_scheduled_task(
                query, interval_hours, run_immediately, model, pipeline
            )

        elif action == "run":
            query = params.get("query", "")
            model_name = params.get("model_name")
            pipeline = params.get("pipeline")  # Pipeline preference
            # Resolve model from jiuwenswarm config
            model = _resolve_model(ctx, model_name)
            payload = await ctx.services.scheduler_service.run_task(query, model, pipeline)

        elif action == "list":
            tasks = await ctx.services.scheduler_service.list_scheduled_tasks()
            payload = {"tasks": tasks}

        elif action == "status":
            task_id = params.get("task_id", "")
            task = await ctx.services.scheduler_service.get_scheduled_task_status(task_id)
            payload = task if task else {"error": "任务不存在", "task_id": task_id}

        elif action == "logs":
            task_id = params.get("task_id", "")
            log_type = params.get("log_type", "current")
            # 归一化数字型参数
            history_index = _coerce_int(params.get("history_index"), -1)
            offset = _coerce_int(params.get("offset"), 0)
            limit = _coerce_int(params.get("limit"), 500)
            payload = await ctx.services.scheduler_service.get_scheduled_task_logs(
                task_id, log_type, history_index, offset, limit
            )

        elif action == "cancel":
            task_id = params.get("task_id", "")
            payload = await ctx.services.scheduler_service.cancel_scheduled_task(task_id)

        elif action == "delete":
            task_id = params.get("task_id", "")
            payload = await ctx.services.scheduler_service.delete_scheduled_task(task_id)

        elif action == "issue_watch_once":
            model_name = params.get("model_name")
            model = _resolve_model(ctx, model_name)
            payload = await ctx.services.scheduler_service.watch_gitcode_issues_once(params, model)

        elif action == "issue_state_list":
            payload = await ctx.services.scheduler_service.list_gitcode_issue_states()

        elif action == "issue_delete":
            payload = await ctx.services.scheduler_service.delete_issue_states(params)

        elif action == "issue_matrix":
            payload = await ctx.services.scheduler_service.refresh_issue_matrix(params)

        else:
            payload = {"error": f"未知的调度操作: {action}"}

        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=payload,
        )
        logger.info(
            "[AgentServer] schedule.%s response prepared: request_id=%s channel_id=%s ok=%s payload_keys=%s",
            action, resp.request_id, resp.channel_id, resp.ok, list(payload.keys())[:10],
        )
    except Exception as exc:
        logger.exception("[AgentServer] schedule.%s failed: %s", action, exc)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(exc)},
        )

    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    logger.info(
        "[AgentServer] schedule.%s sending response wire: request_id=%s wire_keys=%s",
        action, request.request_id, list(wire.keys())[:10],
    )
    await ctx.sink.send_wire(wire)
