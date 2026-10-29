# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Schedule management; resources remain owned by its injected host."""

from __future__ import annotations

import logging
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.agent import AgentResponse
from typing import Any
from typing import Optional
from jiuwenswarm.runtime.request import (
    apply_resolved_mode_to_request as _apply_resolved_mode_to_request,
)
import asyncio
from jiuwenswarm.common.config import get_available_models
from jiuwenswarm.common.config import get_config
from jiuwenswarm.runtime.request import resolve_request_project_dir

logger = logging.getLogger(__name__)


def create_auto_harness_service(*args: Any, **kwargs: Any) -> Any:
    """Construct the optional service at its first RPC, outside startup."""
    from jiuwenswarm.agents.harness.common.auto_harness import (
        AutoHarnessService as Service,
    )

    return Service(*args, **kwargs)


class ScheduleManagement:
    def __init__(self, host):
        self.host = host

    def _resolve_model(self, model_name: Optional[str] = None) -> Optional[Any]:
        """Resolve model from jiuwenswarm config.

        Args:
            model_name: Requested model name, falls back to default if None or not found

        Returns:
            Model instance or None if config cannot be loaded
        """
        host = self.host
        if not host._model_cache:
            host._build_model_cache()
        if model_name and model_name in host._model_cache:
            return host._model_cache[model_name]
        if model_name:
            host._build_model_cache()
            if model_name in host._model_cache:
                return host._model_cache[model_name]
            logger.warning(
                "[_resolve_model] 模型 %r 不在可用列表里，回退到默认模型；可用模型: %s",
                model_name,
                sorted(host._model_cache),
            )
        return host._default_model

    def _build_model_cache(self) -> None:
        """Build model cache from jiuwenswarm config.yaml (reuse interface_deep logic)."""
        host = self.host
        from jiuwenswarm.server.runtime.agent_adapter.model_factory import (
            build_model_from_entry,
        )

        config = get_config()
        for entry in get_available_models(config):
            mcc = entry.get("model_client_config") or {}
            model_name = mcc.get("model_name")
            if not model_name:
                continue
            mco = entry.get("model_config_obj") or {}
            host._model_cache[model_name] = build_model_from_entry(mcc, mco)
        if not host._model_cache:
            default_model_config = config.get("models", {}).get("default", {})
            react_config = config.get("react", {})
            mcc = dict(
                default_model_config.get("model_client_config")
                or react_config.get("model_client_config")
                or {}
            )
            model_name = (
                mcc.get("model_name") or react_config.get("model_name") or "gpt-4"
            )
            if "model_name" not in mcc:
                mcc["model_name"] = model_name
            mco = (
                default_model_config.get("model_config_obj")
                or react_config.get("model_config_obj")
                or {}
            )
            host._model_cache[model_name] = build_model_from_entry(mcc, mco)
        if host._model_cache:
            first_name = next(iter(host._model_cache))
            host._default_model = host._model_cache[first_name]
            logger.info(
                "[AgentServer] Built model cache with %d models, default=%s",
                len(host._model_cache),
                first_name,
            )
        try:
            from jiuwenswarm.server.runtime.opencode_zen import (
                get_zen_free_model_entries,
            )

            for zent in get_zen_free_model_entries():
                zmcc = zent.get("model_client_config") or {}
                zname = zmcc.get("model_name", "")
                if zname and zname not in host._model_cache:
                    host._model_cache[zname] = build_model_from_entry(
                        zmcc, zent.get("model_config_obj") or {}
                    )
        except Exception:
            logger.debug(
                "[AgentServer] append zen free models to cache failed", exc_info=True
            )
        if host._default_model is not None:
            try:
                from jiuwenswarm.common.model_config_validation import (
                    is_placeholder_model_entry,
                    model_client_config_view,
                )
                from jiuwenswarm.server.runtime.opencode_zen import (
                    get_zen_default_free_model_entry,
                )

                if is_placeholder_model_entry(
                    model_client_config_view(host._default_model.model_client_config)
                ):
                    zen_default = get_zen_default_free_model_entry()
                    if zen_default is not None:
                        zmcc = zen_default["model_client_config"]
                        zname = zmcc["model_name"]
                        host._model_cache[zname] = build_model_from_entry(
                            zmcc, zen_default.get("model_config_obj") or {}
                        )
                        host._default_model = host._model_cache[zname]
                        logger.info(
                            "[AgentServer] default model is placeholder; fallback to zen free model=%s",
                            zname,
                        )
            except Exception:
                logger.debug(
                    "[AgentServer] fallback default to zen free model failed",
                    exc_info=True,
                )

    async def _handle_schedule_request(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock, action: str
    ) -> None:
        """Handle schedule.* requests - schedule task management."""
        host = self.host
        logger.info(
            "[AgentServer] schedule.%s request received: request_id=%s channel_id=%s",
            action,
            request.request_id,
            request.channel_id,
        )
        try:
            if host._scheduler_service is None:
                logger.info(
                    "[AgentServer] Initializing scheduler service on first request"
                )
                host._scheduler_service = create_auto_harness_service(None, agent=None)
                await host._scheduler_service.start_scheduler()
            params = request.params or {}
            payload: dict[str, Any] = {}
            needs_agent = action in (
                "create",
                "run",
                "cancel",
                "delete",
                "issue_watch_once",
            )
            if needs_agent:
                mode, sub_mode = _apply_resolved_mode_to_request(request)
                agent_mode = "agent" if mode == "auto_harness" else mode
                agent = await host._agent_manager.get_agent(
                    channel_id=request.channel_id or "tui",
                    mode=agent_mode,
                    project_dir=resolve_request_project_dir(request),
                    sub_mode=sub_mode,
                )
                if agent is None:
                    raise ValueError("Failed to get agent for schedule request")
                await host._scheduler_service.update_agent_instance(agent)
                host._set_scheduler_agent(agent)
                logger.info(
                    "[AgentServer] Set agent for schedule action %s: %s",
                    action,
                    agent is not None,
                )
            if action == "check_config":
                payload = host._scheduler_service.check_schedule_config()
            elif action == "update_config":
                fields = params.get("fields", {})
                payload = host._scheduler_service.update_schedule_config(fields)
            elif action == "create":
                query = params.get("query", "")
                interval_hours = params.get("interval_hours", 4)
                run_immediately = params.get("run_immediately", False)
                model_name = params.get("model_name")
                pipeline = params.get("pipeline")
                model = host._resolve_model(model_name)
                payload = await host._scheduler_service.create_scheduled_task(
                    query, interval_hours, run_immediately, model, pipeline
                )
            elif action == "run":
                query = params.get("query", "")
                model_name = params.get("model_name")
                pipeline = params.get("pipeline")
                model = host._resolve_model(model_name)
                payload = await host._scheduler_service.run_task(query, model, pipeline)
            elif action == "list":
                tasks = await host._scheduler_service.list_scheduled_tasks()
                payload = {"tasks": tasks}
            elif action == "status":
                task_id = params.get("task_id", "")
                task = await host._scheduler_service.get_scheduled_task_status(task_id)
                payload = task if task else {"error": "任务不存在", "task_id": task_id}
            elif action == "logs":
                task_id = params.get("task_id", "")
                log_type = params.get("log_type", "current")
                history_index = params.get("history_index", -1)
                offset = params.get("offset", 0)
                limit = params.get("limit", 500)
                payload = await host._scheduler_service.get_scheduled_task_logs(
                    task_id, log_type, history_index, offset, limit
                )
            elif action == "cancel":
                task_id = params.get("task_id", "")
                payload = await host._scheduler_service.cancel_scheduled_task(task_id)
            elif action == "delete":
                task_id = params.get("task_id", "")
                payload = await host._scheduler_service.delete_scheduled_task(task_id)
            elif action == "issue_watch_once":
                model_name = params.get("model_name")
                model = host._resolve_model(model_name)
                payload = await host._scheduler_service.watch_gitcode_issues_once(
                    params, model
                )
            elif action == "issue_state_list":
                payload = await host._scheduler_service.list_gitcode_issue_states()
            elif action == "issue_delete":
                payload = await host._scheduler_service.delete_issue_states(params)
            elif action == "issue_matrix":
                payload = await host._scheduler_service.refresh_issue_matrix(params)
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
                action,
                resp.request_id,
                resp.channel_id,
                resp.ok,
                list(payload.keys())[:10],
            )
        except Exception as exc:
            logger.exception("[AgentServer] schedule.%s failed: %s", action, exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc)},
            )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        logger.info(
            "[AgentServer] schedule.%s sending response wire: request_id=%s wire_keys=%s",
            action,
            request.request_id,
            list(wire.keys())[:10],
        )
        async with send_lock:
            await host.services.send(ws, wire)
