# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workflow Control management; resources remain owned by its injected host."""

from __future__ import annotations

import logging
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.agent import AgentResponse
from typing import Any
import asyncio

logger = logging.getLogger(__name__)


class WorkflowControlManagement:
    def __init__(self, host):
        self.host = host

    async def _handle_swarmflow_pause(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Handle swarmflow.pause RPC — pause a live swarmflow run by run_id."""
        host = self.host
        await host._run_swarmflow_control(ws, request, send_lock, action="pause")

    async def _handle_swarmflow_resume(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Handle swarmflow.resume RPC — resume a paused swarmflow run by run_id."""
        host = self.host
        await host._run_swarmflow_control(ws, request, send_lock, action="resume")

    async def _handle_swarmflow_stop(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Handle swarmflow.stop RPC — stop a swarmflow run by run_id."""
        host = self.host
        await host._run_swarmflow_control(ws, request, send_lock, action="stop")

    async def _run_swarmflow_control(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock, *, action: str
    ) -> None:
        """Shared pause/resume/stop control-path handler for a swarmflow run.

        Looks up the session's BackgroundTaskController and applies the requested
        control to the run identified by ``run_id`` (accepting ``run_id`` or the
        ``workflow_run_id`` alias). Returns ok=False with a reason when run_id is
        missing, the team is asleep (no leader harness to host the run — the
        tree-view buttons are greyed then), or no matching run
        is registered on the controller.
        """
        host = self.host
        from jiuwenswarm.server.runtime.agent_adapter.team_helpers import (
            classify_swarmflow_control_miss,
            get_background_task_controller,
        )

        session_id = request.session_id or ""
        channel_id = request.channel_id or "web"
        params = request.params if isinstance(request.params, dict) else {}
        run_id = params.get("run_id") or params.get("workflow_run_id")

        from openjiuwen.agent_teams.runtime.pool import RuntimeState
        from openjiuwen.core.runner import Runner

        from jiuwenswarm.agents.harness.team import get_team_manager

        tm = get_team_manager(channel_id)
        active_teams = await Runner.list_active_teams()
        team_runtime_running = any(
            (
                str(info.current_session_id or "") == session_id
                and info.state == RuntimeState.RUNNING
                for info in active_teams
            )
        )
        if not isinstance(run_id, str) or not run_id.strip():
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=channel_id,
                ok=False,
                payload={"error": "run_id is required"},
            )
        elif not team_runtime_running:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=channel_id,
                ok=False,
                payload={"error": "工作流控制不可用，发送一条消息唤醒团队后即可使用。"},
            )
        else:
            run_id = run_id.strip()
            controller = get_background_task_controller(session_id)
            status = {"pause": "paused", "resume": "resumed", "stop": "stopped"}.get(
                action, ""
            )
            if action == "pause":
                acted = await controller.pause(run_id)
            elif action == "resume":
                acted = await controller.resume(run_id)
            elif action == "stop":
                acted = await controller.stop(run_id)
                wf_handler = tm.get_workflow_handler(session_id)
                if wf_handler is not None and await wf_handler.stop_run(run_id):
                    acted = True
            else:
                acted = False
            if not acted:
                miss = classify_swarmflow_control_miss(channel_id, session_id, run_id)
                if miss is not None:
                    resp = AgentResponse(
                        request_id=request.request_id,
                        channel_id=channel_id,
                        ok=False,
                        payload=miss,
                    )
                else:
                    resp = AgentResponse(
                        request_id=request.request_id,
                        channel_id=channel_id,
                        ok=False,
                        payload={"error": "workflow run not found"},
                    )
            else:
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=channel_id,
                    ok=True,
                    payload={"run_id": run_id, "status": status},
                )
        logger.info(
            "[SWARMFLOW] %s req channel_id=%s session_id=%s request_id=%s run_id=%s ok=%s",
            action,
            channel_id,
            session_id,
            request.request_id,
            run_id,
            resp.ok,
        )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)
