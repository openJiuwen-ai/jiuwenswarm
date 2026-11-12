# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Text-only application conversations using product model and history services.

No DeepAgent is assembled here: host tools, project instructions, extensions,
and memory rails cannot contribute to the final model input. Lead sessions expose one
``ask_<member>`` tool per roster member that routes the question into the
member's own app_role session (see ``AppRoleMemberExecutor``).
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Any

from openjiuwen.core.foundation.llm.schema.message import (
    AssistantMessage,
    SystemMessage,
    ToolMessage,
    UserMessage,
)
from openjiuwen.core.foundation.llm.schema.message_chunk import AssistantMessageChunk

from jiuwenswarm.common.config import get_config
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.app_role import (
    app_role_instructions,
    app_role_npc_team,
    app_role_system_prompt,
    validate_app_role_member,
    validate_app_role_request,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
from jiuwenswarm.server.runtime.session.session_history import (
    append_history_record,
    flush_history_writes,
    load_history_records,
)
from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata

MAX_CONTEXT_CHARS = 48_000
MAX_LEADER_TOOL_ROUNDS = 5
MAX_MEMBER_QUESTION_CHARS = 8_000


def build_role_messages(
    system: str, records: list[dict[str, Any]], query: str, request_id: str,
) -> list[Any]:
    """Keep recent completed dialogue pairs, never host events or other roles."""
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


def _member_tool_definition(name: str, desc: str) -> dict[str, Any]:
    function = {
        "name": f"ask_{name}",
        "description": f"Consult the supporting character '{name}'. {desc}"[:1024],
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "A self-contained question for this character.",
                }
            },
            "required": ["question"],
        },
    }
    return {"type": "function", "function": function}


class AppRoleMemberExecutor:
    """Run one consultation against a member's own app_role session.

    The member processes the question through the standard app_role path
    (its own rebuilt history and no host tools) and the exchange
    is persisted into the member session's history with the same record shape
    as an external chat, so the session page shows every consultation.
    """

    def __init__(self, facade: JiuWenSwarmDeepAdapter, leader_session_id: str) -> None:
        self._facade = facade
        self._leader_session_id = leader_session_id

    async def ask(
        self, member_session_id: str, name: str, question: str, channel_id: str,
    ) -> tuple[str, dict[str, Any] | None]:
        """Return ``(answer, usage)``; raises when the member cannot answer."""
        question = str(question or "").strip()[:MAX_MEMBER_QUESTION_CHARS]
        if not question:
            raise ValueError("empty consultation question")
        member_metadata = await asyncio.to_thread(
            get_session_metadata, member_session_id, cache_bust=True, enable_writeback=False,
        )
        binding = {"leader_session_id": self._leader_session_id, "name": name}
        validate_app_role_member(member_metadata, binding)
        adapter = await self._facade._get_or_create_session_adapter(
            member_session_id, required_app_role_member=binding,
        )
        member_request_id = str(uuid.uuid4())
        request = AgentRequest(
            request_id=member_request_id,
            channel_id=channel_id,
            session_id=member_session_id,
            req_method=ReqMethod.CHAT_SEND,
            params={"query": question, "mode": member_metadata.get("mode") or "agent"},
            is_stream=True,
            timestamp=time.time(),
        )
        inputs = {
            "conversation_id": member_session_id,
            "query": question,
            "enable_memory": False,
            "supports_user_interaction": False,
        }
        await asyncio.to_thread(
            append_history_record,
            session_id=member_session_id,
            request_id=member_request_id,
            channel_id=channel_id,
            role="user",
            content=question,
            timestamp=time.time(),
            mode=member_metadata.get("mode"),
        )
        answer = ""
        usage: dict[str, Any] | None = None
        async with aclosing(adapter.process_message_stream_impl(request, inputs)) as stream:
            async for chunk in stream:
                payload = chunk.payload or {}
                if payload.get("event_type") == "chat.final":
                    answer = str(payload.get("content") or "")
                elif payload.get("event_type") == "chat.usage_summary":
                    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else None
        if not answer:
            raise ValueError(f"member '{name}' returned an empty answer")
        await asyncio.to_thread(
            append_history_record,
            session_id=member_session_id,
            request_id=member_request_id,
            channel_id=channel_id,
            role="assistant",
            event_type="chat.final",
            content=answer,
            timestamp=time.time(),
            extra={"completion_state": "completed", "npc_member_reply": True},
            mode=member_metadata.get("mode"),
        )
        return answer, usage


class AppRoleAdapter(JiuWenSwarmDeepAdapter):
    """Reuse product model selection and lifecycle without general-agent prompts."""

    def __init__(self) -> None:
        super().__init__()
        self._member_executor: AppRoleMemberExecutor | None = None

    def bind_member_executor(self, executor: AppRoleMemberExecutor | None) -> None:
        """Attach the facade-owned executor used by ask_<member> tool calls."""
        self._member_executor = executor

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

    def _role_tool_definitions(self, metadata: dict[str, Any]) -> list[dict[str, Any]]:
        """Expose only consultation tools for the fixed member roster."""
        definitions: list[dict[str, Any]] = []
        is_member = bool(metadata.get("npc_member"))
        if not is_member:
            bindings = metadata.get("npc_member_sessions")
            npc_team = app_role_npc_team(metadata)
            if npc_team is not None and isinstance(bindings, dict):
                for member in npc_team["members"]:
                    sid = str(bindings.get(member["name"]) or "")
                    if sid and self._member_executor is not None:
                        definitions.append(_member_tool_definition(member["name"], member["desc"]))
        return definitions

    async def _execute_role_tool_call(
        self,
        call: Any,
        metadata: dict[str, Any],
        request: AgentRequest,
    ) -> tuple[ToolMessage, list[dict[str, Any]]]:
        """Dispatch one tool call; return its reply message and UI events.

        Events are plain payloads (``npc.member`` phases) that the caller
        yields as chunks; the tool reply itself stays in-memory for the model
        round and is never forwarded to clients.
        """
        name = str(getattr(call, "name", "") or "")
        call_id = str(getattr(call, "id", "") or "")
        try:
            arguments = json.loads(str(getattr(call, "arguments", "") or "{}"))
        except ValueError as exc:
            raise ValueError(f"invalid tool arguments for {name}: {exc}") from exc
        if name.startswith("ask_"):
            if self._member_executor is None:
                raise ValueError(f"app_role does not support tool: {name}")
            bindings = metadata.get("npc_member_sessions")
            member_name = name[len("ask_"):]
            member_sid = (
                str(bindings.get(member_name) or "")
                if isinstance(bindings, dict) else ""
            )
            if not member_sid:
                raise ValueError(f"member '{member_name}' is not available")
            question = str(arguments.get("question") or "")
            events: list[dict[str, Any]] = [
                {
                    "event_type": "npc.member",
                    "member": member_name,
                    "member_session_id": member_sid,
                    "phase": "started",
                }
            ]
            try:
                answer, usage = await self._member_executor.ask(
                    member_sid, member_name, question, request.channel_id or "web",
                )
            except Exception as exc:
                events.append(
                    {
                        "event_type": "npc.member",
                        "member": member_name,
                        "member_session_id": member_sid,
                        "phase": "finished",
                        "error": str(exc),
                    }
                )
                return (
                    ToolMessage(
                        tool_call_id=call_id,
                        content=f"Member '{member_name}' could not answer: {exc}",
                    ),
                    events,
                )
            events.append(
                {
                    "event_type": "npc.member",
                    "member": member_name,
                    "member_session_id": member_sid,
                    "phase": "finished",
                    "usage": usage or {},
                    "summary": answer[:500],
                }
            )
            return ToolMessage(tool_call_id=call_id, content=answer), events
        raise ValueError(f"app_role does not support tool: {name}")

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
                system = app_role_system_prompt(metadata) + "\nApplication character instructions:\n" + instructions
                tool_definitions = self._role_tool_definitions(metadata)
                max_tool_rounds = MAX_LEADER_TOOL_ROUNDS if tool_definitions else 0
                config = get_config()
                self._create_model(config)
                model = self._resolve_model_for_request(request)
                await asyncio.to_thread(flush_history_writes)
                records = await asyncio.to_thread(load_history_records, sid)
                messages: list[Any] = build_role_messages(system, records, query, request.request_id)
                content: list[str] = []
                usage = None
                for _tool_round in range(max_tool_rounds + 1):
                    stream = model.stream(
                        messages=messages,
                        tools=tool_definitions or None,
                    )
                    accumulated: AssistantMessageChunk | None = None
                    round_content: list[str] = []
                    async with aclosing(stream):
                        async for chunk in stream:
                            if chunk.tool_calls:
                                accumulated = (
                                    chunk if accumulated is None else accumulated + chunk
                                )
                                continue
                            if chunk.usage_metadata is not None:
                                usage = chunk.usage_metadata
                            if chunk.content:
                                if not isinstance(chunk.content, str):
                                    raise ValueError("app_role only supports text responses")
                                round_content.append(chunk.content)
                                content.append(chunk.content)
                                yield AgentResponseChunk(
                                    request_id=request.request_id, channel_id=request.channel_id,
                                    payload={"event_type": "chat.delta", "content": chunk.content},
                                )
                    tool_calls = accumulated.tool_calls if accumulated is not None else None
                    if not tool_calls:
                        break
                    if not tool_definitions:
                        # No tools were offered this session: an unexpected tool
                        # call stays an explicit error, never an execution path.
                        raise ValueError("app_role does not execute tool calls")
                    if _tool_round >= max_tool_rounds:
                        raise ValueError("app_role exceeded its tool call budget")
                    # One assistant message carries the round's streamed text
                    # (if any) together with its tool calls, matching the wire
                    # protocol shape providers expect for tool rounds.
                    messages.append(
                        AssistantMessage(
                            content="".join(round_content),
                            tool_calls=tool_calls,
                        )
                    )
                    for call in tool_calls:
                        tool_message, events = await self._execute_role_tool_call(
                            call, metadata, request,
                        )
                        for event in events:
                            yield AgentResponseChunk(
                                request_id=request.request_id,
                                channel_id=request.channel_id,
                                payload=event,
                            )
                        messages.append(tool_message)
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
