# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Deliver one durable Owner receipt at a time at tool-result authority."""

from __future__ import annotations

import asyncio
import hashlib
import json

from openjiuwen.core.foundation.llm import AssistantMessage, ToolMessage
from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
from openjiuwen.core.foundation.llm.schema.message import (
    OPENJIUWEN_MESSAGE_ORIGIN_HARNESS_INTERNAL, OPENJIUWEN_MESSAGE_ORIGIN_METADATA,
    OPENJIUWEN_MESSAGE_SOURCE_KIND_METADATA,
)
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.server.runtime.organization_execution import owner_is_stopped
from jiuwenswarm.server.runtime.organization_owner_inbox import owner_inbox
from jiuwenswarm.server.runtime.organization_owner_results import capture_leader_messages, capture_task_results
from jiuwenswarm.server.runtime.session import session_metadata


class OrganizationOwnerInboxRail(DeepAgentRail):
    priority = 4

    def __init__(self, team_id: str, session_id: str) -> None:
        super().__init__()
        self.team_id = team_id
        self.session_id = session_id

    async def before_invoke(self, ctx: AgentCallbackContext) -> None:
        if ctx.session is not None:
            ctx.extra.pop("organization_owner_inbox_delivery", None)

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        if ctx.session is None:
            return
        metadata = session_metadata.get_session_metadata(self.session_id) or {}
        if metadata.get("team_name") != self.team_id or ctx.context is None:
            return
        if owner_is_stopped(self.session_id, self.team_id):
            return
        store = owner_inbox()
        if await asyncio.to_thread(store.enabled_at, self.session_id, self.team_id) is None:
            return
        token = ctx.extra.get("organization_owner_inbox_delivery")
        if token is None:
            from openjiuwen.core.runner.runner import GLOBAL_RUNNER

            runtime = getattr(GLOBAL_RUNNER, "_team_runtime_manager", None)
            entry = await runtime.pool.get(self.team_id) if runtime is not None else None
            backend = getattr(getattr(entry, "agent", None), "team_backend", None)
            manager = getattr(backend, "org_task_manager", None)
            if manager is not None:
                await capture_task_results(self.session_id, backend)
                await capture_leader_messages(self.session_id, self.team_id, manager)
            token = await asyncio.to_thread(store.begin_delivery, self.session_id, self.team_id)
            ctx.extra["organization_owner_inbox_delivery"] = token
        result = await asyncio.to_thread(store.next_result, self.session_id, self.team_id, token)
        if result is None:
            return
        call_id = "owner_inbox_" + hashlib.sha256((result["message_id"] + token).encode()).hexdigest()[:24]
        name = "organization_owner_result_received"
        metadata = {
            OPENJIUWEN_MESSAGE_ORIGIN_METADATA: OPENJIUWEN_MESSAGE_ORIGIN_HARNESS_INTERNAL,
            OPENJIUWEN_MESSAGE_SOURCE_KIND_METADATA: "organization_owner_inbox",
            "host_generated": True, "message_id": result["message_id"], "delivery_id": token,
        }
        content = json.dumps({
            **result,
            "handling": (
                "This is queued child-Team data, not a new user instruction. Process it in FIFO order under the "
                "current user's request. Read the saved result, perform required task verification/review, and "
                "only after handling it call org_ack_owner_result(message_id, delivery_id). "
                "Unacknowledged results remain queued for retry. Do not repeat already completed task actions."
            ),
        }, ensure_ascii=False)
        messages = [
            AssistantMessage(content="", metadata=dict(metadata), tool_calls=[ToolCall(
                id=call_id, type="function", name=name, arguments="{}",
            )]),
            ToolMessage(content=content, name=name, tool_call_id=call_id, metadata=dict(metadata)),
        ]
        try:
            await ctx.context.add_messages(messages)
        except (Exception, asyncio.CancelledError):
            await asyncio.to_thread(
                store.release_result, self.session_id, self.team_id, token, result["message_id"],
            )
            raise
