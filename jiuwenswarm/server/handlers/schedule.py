# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""定时任务和 issue。

定时任务是到点自动跑一次的工作。issue 是盯着问题单的状态。
这两类请求共用一个入口，用请求类型区分具体动作。
调度器进程仍挂在现有服务器上，停服务时的收尾也留在那里。
"""

from __future__ import annotations

import logging
from typing import Any

from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.context import RequestContext

logger = logging.getLogger(__name__)

# 请求类型对应原来的 action 字符串。日志和调度器仍认这些名字。
_ACTIONS = {
    ReqMethod.SCHEDULE_CHECK_CONFIG: "check_config",
    ReqMethod.SCHEDULE_UPDATE_CONFIG: "update_config",
    ReqMethod.SCHEDULE_CREATE: "create",
    ReqMethod.SCHEDULE_RUN: "run",
    ReqMethod.SCHEDULE_LIST: "list",
    ReqMethod.SCHEDULE_STATUS: "status",
    ReqMethod.SCHEDULE_LOGS: "logs",
    ReqMethod.SCHEDULE_CANCEL: "cancel",
    ReqMethod.SCHEDULE_DELETE: "delete",
    ReqMethod.ISSUE_WATCH_ONCE: "issue_watch_once",
    ReqMethod.ISSUE_STATE_LIST: "issue_state_list",
    ReqMethod.ISSUE_DELETE: "issue_delete",
    ReqMethod.ISSUE_MATRIX: "issue_matrix",
}


async def handle_schedule_request(ctx: RequestContext) -> None:
    """处理定时任务和 issue 请求。"""
    request = ctx.request
    services = ctx.services
    action = _ACTIONS.get(request.req_method, "")
    logger.info(
        "[AgentServer] schedule.%s request received: request_id=%s channel_id=%s",
        action, request.request_id, request.channel_id,
    )
    try:
        # 第一次用到时才把调度器拉起来。
        if services.scheduler_service is None:
            logger.info("[AgentServer] Initializing scheduler service on first request")
            from jiuwenswarm.agents.harness.common.auto_harness import AutoHarnessService

            services.scheduler_service = AutoHarnessService(None, agent=None)
            await services.scheduler_service.start_scheduler()

        params = request.params or {}
        payload: dict[str, Any] = {}
        scheduler = services.scheduler_service

        # 创建、立刻跑、取消、删除、盯一次问题单，都要先拿到当前 Agent。
        needs_agent = action in ("create", "run", "cancel", "delete", "issue_watch_once")
        if needs_agent:
            from jiuwenswarm.runtime.request import (
                apply_resolved_mode_to_request,
                resolve_request_project_dir,
            )

            mode, sub_mode = apply_resolved_mode_to_request(request)
            agent_mode = "agent" if mode == "auto_harness" else mode
            agent = await services.agent_manager.get_agent(
                channel_id=request.channel_id or "tui",
                mode=agent_mode,
                project_dir=resolve_request_project_dir(request),
                sub_mode=sub_mode,
            )
            if agent is None:
                raise ValueError("Failed to get agent for schedule request")
            await scheduler.update_agent_instance(agent)
            services.set_scheduler_agent(agent)
            logger.info("[AgentServer] Set agent for schedule action %s: %s", action, agent is not None)

        if action == "check_config":
            payload = scheduler.check_schedule_config()

        elif action == "update_config":
            fields = params.get("fields", {})
            payload = scheduler.update_schedule_config(fields)

        elif action == "create":
            query = params.get("query", "")
            interval_hours = params.get("interval_hours", 4)
            run_immediately = params.get("run_immediately", False)
            model_name = params.get("model_name")
            pipeline = params.get("pipeline")
            model = services.resolve_model(model_name)
            payload = await scheduler.create_scheduled_task(
                query, interval_hours, run_immediately, model, pipeline
            )

        elif action == "run":
            query = params.get("query", "")
            model_name = params.get("model_name")
            pipeline = params.get("pipeline")
            model = services.resolve_model(model_name)
            payload = await scheduler.run_task(query, model, pipeline)

        elif action == "list":
            tasks = await scheduler.list_scheduled_tasks()
            payload = {"tasks": tasks}

        elif action == "status":
            task_id = params.get("task_id", "")
            task = await scheduler.get_scheduled_task_status(task_id)
            payload = task if task else {"error": "任务不存在", "task_id": task_id}

        elif action == "logs":
            task_id = params.get("task_id", "")
            log_type = params.get("log_type", "current")
            history_index = params.get("history_index", -1)
            offset = params.get("offset", 0)
            limit = params.get("limit", 500)
            payload = await scheduler.get_scheduled_task_logs(
                task_id, log_type, history_index, offset, limit
            )

        elif action == "cancel":
            task_id = params.get("task_id", "")
            payload = await scheduler.cancel_scheduled_task(task_id)

        elif action == "delete":
            task_id = params.get("task_id", "")
            payload = await scheduler.delete_scheduled_task(task_id)

        elif action == "issue_watch_once":
            model_name = params.get("model_name")
            model = services.resolve_model(model_name)
            payload = await scheduler.watch_gitcode_issues_once(params, model)

        elif action == "issue_state_list":
            payload = await scheduler.list_gitcode_issue_states()

        elif action == "issue_delete":
            payload = await scheduler.delete_issue_states(params)

        elif action == "issue_matrix":
            payload = await scheduler.refresh_issue_matrix(params)

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

    await ctx.sink.send_response(resp)
