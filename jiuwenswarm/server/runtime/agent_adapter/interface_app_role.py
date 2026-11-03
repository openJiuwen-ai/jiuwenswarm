# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Text-only application conversations using product model and history services.

No DeepAgent is assembled here: host tools, project instructions, extensions,
and memory rails cannot contribute to the final model input.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Any

from openjiuwen.core.foundation.llm.schema.message import AssistantMessage, SystemMessage, UserMessage

from jiuwenswarm.common.config import get_config
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse, AgentResponseChunk
from jiuwenswarm.runtime.app_role import APP_ROLE_SYSTEM_PROMPT, app_role_instructions, validate_app_role_request
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
from jiuwenswarm.server.runtime.session.session_history import flush_history_writes, load_history_records
from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata

MAX_CONTEXT_CHARS = 48_000


def build_role_messages(
    instructions: str, records: list[dict[str, Any]], query: str, request_id: str,
) -> list[Any]:
    """Keep recent completed dialogue pairs, never host events or other roles."""
    system = APP_ROLE_SYSTEM_PROMPT + "\nApplication character instructions:\n" + instructions
    remaining = MAX_CONTEXT_CHARS - len(system) - len(query)
    if remaining < 0:
        raise ValueError("app_role input exceeds the conversation size limit")
    pairs = []
    pending = None
    failed_requests = {r.get("request_id") for r in records if r.get("feedback_only") and r.get("status") != "success"}
    for record in records:
        if record.get("request_id") == request_id:
            break
        content = record.get("content")
        if not isinstance(content, str) or not content:
            continue
        if record.get("request_id") in failed_requests:
            continue
        if record.get("role") == "user" and not record.get("event_type"):
            pending = record
        elif (
            pending is not None
            and record.get("role") == "assistant"
            and record.get("event_type") == "chat.final"
            and record.get("request_id") == pending.get("request_id")
            and record.get("completion_state", "completed") == "completed"
        ):
            pairs.append((pending["content"], content))
            pending = None
    recent = []
    for user, assistant in reversed(pairs):
        size = len(user) + len(assistant)
        if size > remaining:
            break
        remaining -= size
        recent.append((UserMessage(content=user), AssistantMessage(content=assistant)))
    return [SystemMessage(content=system), *[m for pair in reversed(recent) for m in pair], UserMessage(content=query)]


class AppRoleAdapter(JiuWenSwarmDeepAdapter):
    """Reuse product model selection and lifecycle without general-agent prompts."""

    async def create_instance(
        self, config: dict[str, Any] | None = None, *, mode: str = "agent",
        sub_mode: str | None = None, agent_definition: dict[str, Any] | None = None,
    ) -> None:
        self._session_instance_config = dict(config or {})
        self._instance_overrides = dict(config or {})
        self._session_instance_mode = mode
        self._session_instance_sub_mode = sub_mode
        self._config_base_cache = get_config()
        self._channel_id = str((config or {}).get("channel_id") or "web")
        self._role_lock = asyncio.Lock()
        self._role_task: asyncio.Task | None = None
        self._role_context_usage: dict[str, Any] = {}

    async def reload_agent_config(
        self, config_base: dict[str, Any] | None = None, env_overrides: dict[str, Any] | None = None,
        target_session_id: str | None = None, reload_scopes: set[str] | None = None,
    ) -> None:
        # A reload may update model configuration, never assemble ordinary rails.
        self._config_base_cache = get_config() if config_base is None else config_base

    async def process_message_stream_impl(
        self, request: AgentRequest, inputs: dict[str, Any],
    ) -> AsyncIterator[AgentResponseChunk]:
        async with self._role_lock:
            sid = request.session_id
            self._role_task = asyncio.current_task()
            self._mark_session_active(sid)
            try:
                metadata = await asyncio.to_thread(get_session_metadata, sid, cache_bust=True, enable_writeback=False)
                validate_app_role_request(metadata, request.params or {})
                instructions = app_role_instructions(metadata)
                if instructions is None:
                    raise ValueError("app_role execution configuration is missing")
                query = inputs.get("query")
                if not isinstance(query, str) or not query.strip():
                    raise ValueError("app_role requires a non-empty text message")
                config = get_config()
                self._create_model(config)
                model = self._resolve_model_for_request(request)
                await asyncio.to_thread(flush_history_writes)
                records = await asyncio.to_thread(load_history_records, sid)
                messages = build_role_messages(instructions, records, query, request.request_id)
                content = []
                usage = None
                stream = model.stream(messages=messages, tools=None)
                async with aclosing(stream):
                    async for chunk in stream:
                        if chunk.tool_calls:
                            raise ValueError("app_role does not execute tool calls")
                        if chunk.usage_metadata is not None:
                            usage = chunk.usage_metadata
                        if chunk.content:
                            if not isinstance(chunk.content, str):
                                raise ValueError("app_role only supports text responses")
                            content.append(chunk.content)
                            yield AgentResponseChunk(
                                request_id=request.request_id, channel_id=request.channel_id,
                                payload={"event_type": "chat.delta", "content": chunk.content},
                            )
                if not content:
                    raise ValueError("app_role model returned an empty response")
                if usage is not None:
                    summary = {key: getattr(usage, key, 0) for key in (
                        "input_tokens", "output_tokens", "total_tokens", "cache_tokens",
                    )}
                    self._role_context_usage = summary
                    yield AgentResponseChunk(
                        request_id=request.request_id, channel_id=request.channel_id,
                        payload={"event_type": "chat.usage_summary", "session_id": sid, "usage": summary},
                    )
                yield AgentResponseChunk(
                    request_id=request.request_id, channel_id=request.channel_id,
                    payload={"event_type": "chat.final", "content": "".join(content),
                             "completion_state": "completed"},
                    is_complete=True,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                yield AgentResponseChunk(
                    request_id=request.request_id, channel_id=request.channel_id,
                    payload={"event_type": "chat.error", "error": str(exc)}, is_complete=True,
                )
            finally:
                self._role_task = None
                self._unmark_session_active(sid)

    async def process_message_impl(self, request: AgentRequest, inputs: dict[str, Any]) -> AgentResponse:
        payload = {}
        async for chunk in self.process_message_stream_impl(request, inputs):
            payload = chunk.payload
        return AgentResponse(
            request_id=request.request_id, channel_id=request.channel_id,
            ok=payload.get("event_type") != "chat.error", payload=payload,
        )

    async def process_interrupt(self, request: AgentRequest) -> AgentResponse:
        intent = (request.params or {}).get("intent", "cancel")
        if intent != "cancel":
            return AgentResponse(
                request_id=request.request_id, channel_id=request.channel_id, ok=False,
                payload={"error": "app_role only supports cancel", "code": "BAD_REQUEST"},
            )
        task = self._role_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            if (request.params or {}).get("wait_for_stop"):
                await asyncio.gather(task, return_exceptions=True)
        return AgentResponse(
            request_id=request.request_id, channel_id=request.channel_id, ok=True,
            payload={"event_type": "interrupt_result", "intent": intent, "success": True},
        )

    async def get_context_usage(self, session_id: str) -> dict[str, Any]:
        return dict(self._role_context_usage)
