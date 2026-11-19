# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Explicit Owner-only processing acknowledgements for queued results."""

import asyncio
import sqlite3
from typing import Any

from openjiuwen.agent_teams.tools.tool_base import TeamTool
from openjiuwen.core.foundation.tool.base import ToolCard
from openjiuwen.harness.tools.base_tool import ToolOutput

from jiuwenswarm.server.runtime.organization_execution import owner_is_stopped
from jiuwenswarm.server.runtime.organization_owner_inbox import owner_inbox
from jiuwenswarm.server.runtime.session import session_metadata


class OrgAckOwnerResultTool(TeamTool):
    def __init__(self, team_id: str, session_id: str) -> None:
        super().__init__(ToolCard(
            id="swarm.org_ack_owner_result", name="org_ack_owner_result",
            description=(
                "Owner only: acknowledge a queued result only after reading it and completing required review "
                "or verification. Use message_id and delivery_id from organization_owner_result_received. "
                "Do not acknowledge on receipt alone. Results are delivered in FIFO order; acknowledgement "
                "unlocks the next result. Repeated acknowledgement is idempotent."
                " For a leader_message, also complete org_ack_leader_message for its original message first."
            ),
            input_params={
                "type": "object", "properties": {
                    "message_id": {"type": "string"}, "delivery_id": {"type": "string"},
                }, "required": ["message_id", "delivery_id"],
            },
        ))
        self.team_id = team_id
        self.session_id = session_id

    async def invoke(self, inputs: dict[str, Any], **kwargs: Any) -> ToolOutput:
        metadata = session_metadata.get_session_metadata(self.session_id) or {}
        if metadata.get("team_name") != self.team_id or owner_is_stopped(self.session_id, self.team_id):
            return ToolOutput(success=False, error="Only the active Owner may acknowledge its result inbox")
        try:
            store = owner_inbox()
            receipt = await asyncio.to_thread(
                store.get_result, self.session_id, self.team_id, inputs.get("message_id", ""),
            )
            if receipt is None:
                return ToolOutput(success=False, error="Unknown Owner inbox receipt")
            if receipt["acknowledged_at"] is None:
                await self._check_source_handled(receipt["payload"])
            acknowledged = await asyncio.to_thread(
                store.acknowledge, self.session_id, self.team_id,
                inputs.get("delivery_id", ""), inputs.get("message_id", ""),
            )
        except (ValueError, OSError, sqlite3.Error) as exc:
            return ToolOutput(success=False, error=str(exc))
        return ToolOutput(success=True, data={
            "acknowledged": acknowledged, "duplicate": receipt["acknowledged_at"] is not None,
        })

    async def _check_source_handled(self, payload: dict[str, Any]) -> None:
        message = payload.get("message") or {}
        native_id = message.get("message_id") or payload.get("leader_message_id")
        task = payload.get("task") or {}
        if not native_id and not task:
            return
        from openjiuwen.core.runner.runner import GLOBAL_RUNNER

        runtime = getattr(GLOBAL_RUNNER, "_team_runtime_manager", None)
        entry = await runtime.pool.get(self.team_id) if runtime is not None else None
        backend = getattr(getattr(entry, "agent", None), "team_backend", None)
        manager = getattr(backend, "org_task_manager", None)
        if manager is None:
            raise ValueError("Owner organization runtime is unavailable")
        if native_id:
            original = await manager.message_service.get_leader_message(message_id=native_id, team_id=self.team_id)
            if original is not None and original.get("handled_at") is None:
                raise ValueError("Handle and acknowledge the original message with org_ack_leader_message first")
        if task and task.get("organization_id") == manager.organization_id:
            review = await manager.task_pool.get_task_review(task["task_id"])
            status = getattr(review, "review_status", "")
            if str(getattr(status, "value", status)).upper() == "PENDING":
                raise ValueError("Complete the task's pending org_review_task review before acknowledging this result")
