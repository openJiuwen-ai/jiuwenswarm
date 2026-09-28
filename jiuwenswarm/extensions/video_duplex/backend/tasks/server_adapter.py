"""AgentServer endpoints for checkpoint ACKs and task-produced files."""

import asyncio
import re

from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.gateway_adapter.base import GatewayAdapter

from .bridge import resolve_checkpoint_ack


class VoiceTaskServerAdapter(GatewayAdapter):
    methods = frozenset({
        ReqMethod.VOICE_TASK_CHECKPOINT_ACK.value, ReqMethod.VOICE_TASK_FILES.value,
    })

    async def handle(self, request):
        if request.req_method == ReqMethod.VOICE_TASK_CHECKPOINT_ACK:
            payload = {"accepted": resolve_checkpoint_ack(request)}
        else:
            from jiuwenswarm.server.runtime.session.lifecycle import guard
            from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata
            from jiuwenswarm.server.runtime.session.session_history import (
                flush_pending_writes, load_history_records,
            )
            from ..video_files import normalize_file_items

            session = request.session_id or ""
            execution_id = (request.params or {}).get("execution_request_id")
            if not re.fullmatch(r"managed-task-[0-9a-f]{32}", session) or not execution_id:
                raise ValueError("Invalid task execution identity")
            guard(session)
            metadata = await asyncio.to_thread(
                get_session_metadata, session, cache_bust=True, enable_writeback=False
            )
            if not metadata or str(metadata.get("user_id") or "").strip() != (request.user_id or ""):
                raise ValueError("Task session is unavailable to this user")
            # Tool completion only enqueues chat.file; wait for the existing
            # history writer before taking the final attachment snapshot.
            if not await asyncio.to_thread(flush_pending_writes, timeout=5):
                raise TimeoutError("Task file history is not yet durable")
            records = await asyncio.to_thread(load_history_records, session)
            files = []
            for record in records:
                if record.get("request_id") == execution_id and record.get("event_type") == "chat.file":
                    files.extend(normalize_file_items(record.get("files")))
            payload = {"files": files}
        return AgentResponse(
            request_id=request.request_id, channel_id=request.channel_id,
            ok=True, payload=payload, metadata=request.metadata,
        )
