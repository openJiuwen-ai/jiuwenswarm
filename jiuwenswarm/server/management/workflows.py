# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workflows management; resources remain owned by its injected host."""

from __future__ import annotations

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.agent import AgentResponse
from typing import Any
from jiuwenswarm.server.wire_truncate import _WORKFLOW_AGENT_DEFAULT_LIMIT
from jiuwenswarm.server.wire_truncate import _WORKFLOW_AGENT_MAX_LIMIT
from jiuwenswarm.server.wire_truncate import _WORKFLOW_LIST_DEFAULT_LIMIT
from jiuwenswarm.server.wire_truncate import _WORKFLOW_LIST_MAX_LIMIT
from jiuwenswarm.server.wire_truncate import _WORKFLOW_PHASE_DEFAULT_LIMIT
from jiuwenswarm.server.wire_truncate import _WORKFLOW_PHASE_MAX_LIMIT
from jiuwenswarm.server.wire_truncate import _build_agent_detail
from jiuwenswarm.server.wire_truncate import _build_phase_detail_paginated
from jiuwenswarm.server.wire_truncate import _build_workflow_detail_paginated
from jiuwenswarm.server.wire_truncate import _build_workflow_list_payload
from jiuwenswarm.server.wire_truncate import _coerce_int
from jiuwenswarm.server.wire_truncate import _json_wire_size
import asyncio
import logging

logger = logging.getLogger(__name__)


class WorkflowsManagement:
    def __init__(self, host):
        self.host = host

    async def _handle_command_workflows(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Handle command.workflows RPC — list summaries or get one workflow detail."""
        host = self.host
        from jiuwenswarm.server.management.workflow_reader import (
            existing_workflow_handler,
            restored_workflows,
        )
    
        session_id = request.session_id or ""
        channel_id = request.channel_id or "web"
        params = request.params if isinstance(request.params, dict) else {}
        action = str(params.get("action") or "list").strip().lower()
        workflow_id = params.get("workflow_id") or params.get("workflow_run_id")
        wf_id_log = workflow_id.strip() if isinstance(workflow_id, str) else workflow_id
        logger.info(
            "[WF_DBG] command.workflows req channel_id=%s session_id=%s request_id=%s action=%s workflow_id=%s",
            channel_id,
            session_id,
            request.request_id,
            action,
            wf_id_log,
        )
        workflow_handler = existing_workflow_handler(session_id)
        source = "live" if workflow_handler is not None else "checkpoint"
        if workflow_handler is None:
            try:
                workflows = restored_workflows(session_id)
            except Exception as exc:
                logger.warning(
                    "[WF_DBG] command.workflows checkpoint_restore_failed session_id=%s error=%s",
                    session_id,
                    exc,
                )
                workflows = []
        else:
            try:
                workflows = workflow_handler.get_workflow_snapshot()
            except Exception as e:
                logger.warning(
                    "[WF_DBG] command.workflows snapshot_failed session_id=%s error=%s",
                    session_id,
                    e,
                )
                workflows = []
        source_count = len(workflows)
        source_bytes = sum(
            (_json_wire_size(item) for item in workflows if isinstance(item, dict))
        )
        target_id = (
            workflow_id.strip()
            if isinstance(workflow_id, str) and workflow_id.strip()
            else None
        )
    
        def _find_workflow() -> dict[str, Any] | None:
            if not target_id:
                return None
            return next(
                (
                    item
                    for item in workflows
                    if isinstance(item, dict) and item.get("id") == target_id
                ),
                None,
            )
    
        if action == "list":
            offset = _coerce_int(
                params.get("offset"), default=0, minimum=0, maximum=10000000
            )
            limit = _coerce_int(
                params.get("limit"),
                default=_WORKFLOW_LIST_DEFAULT_LIMIT,
                minimum=1,
                maximum=_WORKFLOW_LIST_MAX_LIMIT,
            )
            payload = _build_workflow_list_payload(
                workflows,
                session_id=session_id,
                offset=offset,
                limit=limit,
                total=source_count,
            )
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=channel_id,
                ok=True,
                payload=payload,
            )
        elif action == "get_workflow":
            if not target_id:
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=channel_id,
                    ok=False,
                    payload={
                        "error": "workflow_id is required for action=get_workflow"
                    },
                )
            else:
                match = _find_workflow()
                if match is None:
                    resp = AgentResponse(
                        request_id=request.request_id,
                        channel_id=channel_id,
                        ok=False,
                        payload={"error": f"workflow not found: {target_id}"},
                    )
                else:
                    phase_offset = _coerce_int(
                        params.get("phase_offset"),
                        default=0,
                        minimum=0,
                        maximum=10000000,
                    )
                    phase_limit = _coerce_int(
                        params.get("phase_limit"),
                        default=_WORKFLOW_PHASE_DEFAULT_LIMIT,
                        minimum=1,
                        maximum=_WORKFLOW_PHASE_MAX_LIMIT,
                    )
                    payload = _build_workflow_detail_paginated(
                        match,
                        session_id=session_id,
                        phase_offset=phase_offset,
                        phase_limit=phase_limit,
                    )
                    resp = AgentResponse(
                        request_id=request.request_id,
                        channel_id=channel_id,
                        ok=True,
                        payload=payload,
                    )
        elif action == "get_phase":
            phase_id = params.get("phase_id")
            phase_id_str = (
                phase_id.strip()
                if isinstance(phase_id, str) and phase_id.strip()
                else None
            )
            if not target_id or not phase_id_str:
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=channel_id,
                    ok=False,
                    payload={
                        "error": "workflow_id and phase_id are required for action=get_phase"
                    },
                )
            else:
                match = _find_workflow()
                if match is None:
                    resp = AgentResponse(
                        request_id=request.request_id,
                        channel_id=channel_id,
                        ok=False,
                        payload={"error": f"workflow not found: {target_id}"},
                    )
                else:
                    agent_offset = _coerce_int(
                        params.get("agent_offset"),
                        default=0,
                        minimum=0,
                        maximum=10000000,
                    )
                    agent_limit = _coerce_int(
                        params.get("agent_limit"),
                        default=_WORKFLOW_AGENT_DEFAULT_LIMIT,
                        minimum=1,
                        maximum=_WORKFLOW_AGENT_MAX_LIMIT,
                    )
                    payload = _build_phase_detail_paginated(
                        match,
                        session_id=session_id,
                        phase_id=phase_id_str,
                        agent_offset=agent_offset,
                        agent_limit=agent_limit,
                    )
                    resp = AgentResponse(
                        request_id=request.request_id,
                        channel_id=channel_id,
                        ok=payload.get("ok", True),
                        payload=payload,
                    )
        elif action == "get_agent":
            phase_id = params.get("phase_id")
            agent_id = params.get("agent_id")
            phase_id_str = (
                phase_id.strip()
                if isinstance(phase_id, str) and phase_id.strip()
                else None
            )
            agent_id_str = (
                agent_id.strip()
                if isinstance(agent_id, str) and agent_id.strip()
                else None
            )
            if not target_id or not phase_id_str or (not agent_id_str):
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=channel_id,
                    ok=False,
                    payload={
                        "error": "workflow_id, phase_id and agent_id are required for action=get_agent"
                    },
                )
            else:
                match = _find_workflow()
                if match is None:
                    resp = AgentResponse(
                        request_id=request.request_id,
                        channel_id=channel_id,
                        ok=False,
                        payload={"error": f"workflow not found: {target_id}"},
                    )
                else:
                    payload = _build_agent_detail(
                        match,
                        session_id=session_id,
                        phase_id=phase_id_str,
                        agent_id=agent_id_str,
                    )
                    resp = AgentResponse(
                        request_id=request.request_id,
                        channel_id=channel_id,
                        ok=payload.get("ok", True),
                        payload=payload,
                    )
        else:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=channel_id,
                ok=False,
                payload={"error": f"unknown action: {action}"},
            )
        payload = resp.payload if isinstance(resp.payload, dict) else {}
        payload_bytes = _json_wire_size(payload)
        has_more = bool(payload.get("has_more")) if isinstance(payload, dict) else False
        error = (
            payload.get("error")
            if isinstance(payload, dict) and (not resp.ok)
            else None
        )
        log_level = logging.WARNING if not resp.ok or has_more else logging.INFO
        logger.log(
            log_level,
            "[WF_DBG] command.workflows res ok=%s action=%s source=%s session_id=%s workflow_id=%s count=%d source_bytes=%d payload_bytes=%d has_more=%s error=%s",
            resp.ok,
            action,
            source,
            session_id,
            wf_id_log,
            source_count,
            source_bytes,
            payload_bytes,
            has_more,
            error,
        )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)
