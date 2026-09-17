from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

from jiuwenswarm.gateway.channel_manager.base import BaseChannel
from jiuwenswarm.common.e2a.acp.acp_tool_updates import is_reasoning_event
from jiuwenswarm.common.schema.message import EventType, Message, ReqMethod
from jiuwenswarm.gateway.routing.keys import DeliveryTarget
from jiuwenswarm.gateway.routing.session_sharing import RoutingTarget

logger = logging.getLogger(__name__)

try:
    from a2a.server.agent_execution import AgentExecutor as _AgentExecutorBase
except ImportError:
    _AgentExecutorBase = object

# Part-level metadata key marking reasoning/thought content, mirroring the
# convention popularized by Google ADK (`adk_thought`). A2A itself has no
# first-class thought field; `Part.metadata` is the official extension point.
A2A_THOUGHT_METADATA_KEY = "jiuwen_thought"


def _raise_missing_a2a_sdk(exc: ImportError) -> None:
    raise RuntimeError(
        "A2A server is enabled but optional dependency `a2a-sdk[http-server]>=1.0.0` "
        "is not installed. Install with `pip install -e \".[a2a]\"` or `uv sync --extra a2a`."
    ) from exc


@dataclass
class A2AChannelConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 19100
    rpc_path: str = "/a2a"
    card_path: str = "/.well-known/agent-card.json"
    extended_card_path: str = "/agent/authenticatedExtendedCard"
    protocol_version: str = "1.0.0"
    channel_id: str = "a2a"
    app_name: str = "JiuwenSwarm Gateway A2A Server"
    app_description: str = "A2A ingress for JiuwenSwarm Gateway"
    app_version: str = "0.1.0"
    # When True (default), reasoning (thinking) content is streamed to A2A
    # callers as working-state TaskStatusUpdateEvent messages whose parts
    # carry the `jiuwen_thought` metadata marker. When False, reasoning is
    # dropped. Reasoning is never written into the final `response` artifact
    # either way.
    expose_reasoning: bool = True


@dataclass
class _PendingA2ARequest:
    queue: asyncio.Queue[Message]


class _A2AAgentExecutor(_AgentExecutorBase):
    """A2A SDK AgentExecutor that forwards request via channel callback."""

    def __init__(self, channel: "A2AChannel") -> None:
        self._channel = channel

    @staticmethod
    def _resolve_task(context: Any) -> Any:
        from a2a.helpers import new_task, new_task_from_user_message
        from a2a.types import TaskState

        if context.current_task is not None:
            return context.current_task
        if context.message is not None:
            try:
                return new_task_from_user_message(context.message)
            except ValueError:
                task_id = str(
                    context.task_id
                    or getattr(context.message, "task_id", None)
                    or f"a2a_{uuid.uuid4().hex[:12]}"
                )
                context_id = str(
                    context.context_id
                    or getattr(context.message, "context_id", None)
                    or f"a2a_ctx_{uuid.uuid4().hex[:8]}"
                )
                return new_task(task_id, context_id, TaskState.TASK_STATE_SUBMITTED)
        task_id = str(context.task_id or f"a2a_{uuid.uuid4().hex[:12]}")
        context_id = str(context.context_id or f"a2a_ctx_{uuid.uuid4().hex[:8]}")
        return new_task(task_id, context_id, TaskState.TASK_STATE_SUBMITTED)

    async def execute(self, context: Any, event_queue: Any) -> None:
        from a2a.helpers import new_text_status_update_event
        from a2a.types import (
            Artifact,
            Message as A2AMessage,
            Role,
            TaskArtifactUpdateEvent,
            TaskState,
            TaskStatus,
            TaskStatusUpdateEvent,
        )

        task = self._resolve_task(context)
        task_id = str(task.id)
        context_id = str(task.context_id)
        request_id = task_id

        query, files = self._channel.map_a2a_parts_to_params(context.message)
        if not query:
            query = str(context.get_user_input() or "").strip()
        if not query:
            await event_queue.enqueue_event(task)
            await event_queue.enqueue_event(
                new_text_status_update_event(
                    task_id,
                    context_id,
                    TaskState.TASK_STATE_FAILED,
                    "empty query",
                )
            )
            await event_queue.close()
            return

        try:
            await event_queue.enqueue_event(task)
            await event_queue.enqueue_event(
                new_text_status_update_event(
                    task_id,
                    context_id,
                    TaskState.TASK_STATE_WORKING,
                    "Processing...",
                )
            )
            pending = await self._channel.dispatch_a2a_request(
                request_id=request_id,
                session_id=context_id,
                query=query,
                files=files,
                metadata=dict(context.metadata or {}),
            )
            artifact_id = f"{task_id}_response"
            artifact_started = False
            while True:
                response_msg = await pending.queue.get()
                is_terminal = self._channel.is_terminal_message(response_msg)
                is_reasoning = self._channel.is_reasoning_message(response_msg)

                # Reasoning never enters the response artifact. When exposed,
                # it is streamed as working-state status updates with thought
                # metadata so callers can render or ignore it structurally.
                if not is_terminal and is_reasoning:
                    if self._channel.config.expose_reasoning:
                        thought_parts = self._channel.message_to_a2a_parts(
                            response_msg,
                            fallback_to_text=False,
                        )
                        if thought_parts:
                            await event_queue.enqueue_event(
                                TaskStatusUpdateEvent(
                                    task_id=task_id,
                                    context_id=context_id,
                                    status=TaskStatus(
                                        state=TaskState.TASK_STATE_WORKING,
                                        message=A2AMessage(
                                            message_id=f"{task_id}_thought_{uuid.uuid4().hex[:8]}",
                                            task_id=task_id,
                                            context_id=context_id,
                                            role=Role.ROLE_AGENT,
                                            parts=thought_parts,
                                        ),
                                    ),
                                )
                            )
                    continue

                # A terminal reasoning chunk still closes the task but must
                # not leak thinking text into the response artifact.
                response_parts = (
                    []
                    if is_reasoning
                    else self._channel.message_to_a2a_parts(
                        response_msg,
                        fallback_to_text=False,
                    )
                )
                if (
                    is_terminal
                    and not response_parts
                    and response_msg.event_type == EventType.CHAT_ERROR
                ):
                    response_parts = self._channel.message_to_a2a_parts(
                        response_msg,
                        fallback_to_text=True,
                    )
                filtered_parts = []
                for part in response_parts:
                    part_text = str(getattr(part, "text", "") or "").strip()
                    if self._channel.is_completion_sentinel_text(part_text):
                        continue
                    filtered_parts.append(part)
                response_parts = filtered_parts
                if response_parts:
                    await event_queue.enqueue_event(
                        TaskArtifactUpdateEvent(
                            task_id=task_id,
                            context_id=context_id,
                            artifact=Artifact(
                                artifact_id=artifact_id,
                                name="response",
                                parts=response_parts,
                                metadata=response_msg.metadata or None,
                            ),
                            append=artifact_started,
                            last_chunk=is_terminal,
                        )
                    )
                    artifact_started = True
                if is_terminal:
                    if response_msg.event_type == EventType.CHAT_ERROR:
                        final_state = TaskState.TASK_STATE_FAILED
                    elif response_msg.event_type == EventType.CHAT_INTERRUPT_RESULT:
                        final_state = TaskState.TASK_STATE_CANCELED
                    else:
                        final_state = TaskState.TASK_STATE_COMPLETED
                    await event_queue.enqueue_event(
                        TaskStatusUpdateEvent(
                            task_id=task_id,
                            context_id=context_id,
                            status=TaskStatus(state=final_state),
                        )
                    )
                    break
        except Exception as exc:  # noqa: BLE001
            logger.exception("[A2AChannel] execution failed: request_id=%s err=%s", request_id, exc)
            await event_queue.enqueue_event(
                TaskStatusUpdateEvent(
                    task_id=task_id,
                    context_id=context_id,
                    status=TaskStatus(state=TaskState.TASK_STATE_FAILED),
                )
            )
        finally:
            self._channel.clear_pending_request(request_id)
            await event_queue.close()

    async def cancel(self, context: Any, event_queue: Any) -> None:
        from a2a.types import TaskState, TaskStatus, TaskStatusUpdateEvent

        task = context.current_task or self._resolve_task(context)
        task_id = str(context.task_id or task.id)
        context_id = str(context.context_id or task.context_id)
        await event_queue.enqueue_event(task)
        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=task_id,
                context_id=context_id,
                status=TaskStatus(state=TaskState.TASK_STATE_CANCELED),
            )
        )
        await event_queue.close(immediate=True)


class A2AChannel(BaseChannel):
    name = "a2a"

    def __init__(self, config: A2AChannelConfig, router: Any):
        super().__init__(config, router)
        self.config = config
        self._on_message_cb = None
        self._pending: dict[str, _PendingA2ARequest] = {}
        self._uvicorn_server: Any | None = None
        self._server_task: asyncio.Task | None = None

    @property
    def channel_id(self) -> str:
        return str(self.config.channel_id or self.name).strip() or self.name

    def on_message(self, callback) -> None:
        self._on_message_cb = callback

    async def start(self) -> None:
        if self._running:
            return
        if not self.config.enabled:
            logger.info("[A2AChannel] disabled by config")
            return

        try:
            from a2a.server.request_handlers import DefaultRequestHandler
            from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
            from a2a.server.tasks import InMemoryPushNotificationConfigStore, InMemoryTaskStore
            from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
            from fastapi import FastAPI
        except ImportError as exc:
            _raise_missing_a2a_sdk(exc)
        import uvicorn

        agent_card = AgentCard(
            name=self.config.app_name,
            description=self.config.app_description,
            version=self.config.app_version,
            supported_interfaces=[
                AgentInterface(
                    url=f"http://{self.config.host}:{self.config.port}{self.config.rpc_path}",
                    protocol_binding="JSONRPC",
                    protocol_version=self.config.protocol_version,
                )
            ],
            capabilities=AgentCapabilities(streaming=True, push_notifications=False),
            default_input_modes=["text/plain"],
            default_output_modes=["text/plain"],
            skills=[
                AgentSkill(
                    id="chat",
                    name="chat",
                    description="Send user prompt to JiuwenSwarm via Gateway",
                    tags=["chat", "gateway", "jiuwenswarm"],
                    examples=["Hello", "Summarize this"],
                    input_modes=["text/plain"],
                    output_modes=["text/plain"],
                )
            ],
        )
        request_handler = DefaultRequestHandler(
            agent_executor=_A2AAgentExecutor(self),
            task_store=InMemoryTaskStore(),
            agent_card=agent_card,
            push_config_store=InMemoryPushNotificationConfigStore(),
        )
        routes = [
            *create_agent_card_routes(agent_card, card_url=self.config.card_path),
            *create_jsonrpc_routes(request_handler, rpc_url=self.config.rpc_path),
        ]
        fastapi_app = FastAPI(routes=routes)

        uv_cfg = uvicorn.Config(
            app=fastapi_app,
            host=self.config.host,
            port=self.config.port,
            log_level="info",
            access_log=False,
        )
        self._uvicorn_server = uvicorn.Server(uv_cfg)
        self._server_task = asyncio.create_task(self._uvicorn_server.serve(), name="a2a-channel-server")
        await asyncio.sleep(0.2)
        if self._server_task.done():
            exc = self._server_task.exception()
            if exc:
                raise exc
        self._running = True
        logger.info(
            "[A2AChannel] started: http://%s:%s%s",
            self.config.host,
            self.config.port,
            self.config.rpc_path,
        )

    async def stop(self) -> None:
        self._running = False
        if self._uvicorn_server is not None:
            self._uvicorn_server.should_exit = True
        if self._server_task is not None:
            try:
                await self._server_task
            except Exception as exc:  # noqa: BLE001
                logger.warning("[A2AChannel] shutdown with error: %s", exc)
        self._uvicorn_server = None
        self._server_task = None
        for pending in list(self._pending.values()):
            # Wake waiting executors during shutdown.
            await pending.queue.put(
                Message(
                    id="a2a_shutdown",
                    type="event",
                    channel_id=self.channel_id,
                    session_id=None,
                    params={},
                    timestamp=time.time(),
                    ok=False,
                    payload={"error": "a2a channel stopped", "is_complete": True},
                    event_type=EventType.CHAT_ERROR,
                )
            )
        self._pending.clear()
        logger.info("[A2AChannel] stopped")

    async def send(self, msg: Message, *, routing_target: RoutingTarget | None = None) -> None:
        pending = self._pending.get(str(msg.id))
        if pending is None:
            return
        await pending.queue.put(msg)

    async def dispatch_a2a_request(
        self,
        *,
        request_id: str,
        session_id: str | None,
        query: str,
        files: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> _PendingA2ARequest:
        if self._on_message_cb is None:
            raise RuntimeError("A2AChannel on_message callback is not set")
        pending = _PendingA2ARequest(queue=asyncio.Queue())
        self._pending[request_id] = pending
        try:
            msg = Message(
                id=request_id,
                type="req",
                channel_id=self.channel_id,
                session_id=session_id or f"a2a_{uuid.uuid4().hex[:8]}",
                params=self._build_request_params(query=query, files=files),
                timestamp=time.time(),
                ok=True,
                req_method=ReqMethod.CHAT_SEND,
                is_stream=True,
                metadata=dict(metadata or {}),
            )
            result = self._on_message_cb(msg)
            if asyncio.iscoroutine(result):
                await result
            return pending
        finally:
            # Keep pending entry for send() until terminal message is consumed by executor.
            pass

    def clear_pending_request(self, request_id: str) -> None:
        self._pending.pop(str(request_id), None)

    @staticmethod
    def message_to_text(msg: Message) -> str:
        payload = msg.payload if isinstance(msg.payload, dict) else {}
        if msg.type == "event" and msg.event_type == EventType.CHAT_ERROR:
            return str(payload.get("error") or payload.get("content") or "agent request failed")
        if "content" in payload:
            return str(payload.get("content") or "")
        if payload:
            return str(payload)
        return ""

    @staticmethod
    def is_reasoning_message(msg: Message) -> bool:
        """Whether the message carries reasoning (thinking) content."""
        payload = msg.payload if isinstance(msg.payload, dict) else {}
        return is_reasoning_event(msg.event_type, payload)

    @staticmethod
    def message_to_a2a_parts(msg: Message, *, fallback_to_text: bool = True) -> list[Any]:
        """Map internal message payload to A2A response parts."""
        from a2a.types import Part

        payload = msg.payload if isinstance(msg.payload, dict) else {}
        parts: list[Any] = []

        # Keep error response readable for A2A callers.
        if msg.type == "event" and msg.event_type == EventType.CHAT_ERROR:
            error_text = str(payload.get("error") or payload.get("content") or "agent request failed")
            return [Part(text=error_text)]

        thought_metadata = (
            {A2A_THOUGHT_METADATA_KEY: True} if A2AChannel.is_reasoning_message(msg) else None
        )

        content = payload.get("content")
        if isinstance(content, str) and content.strip():
            normalized_content = content.strip()
            if not A2AChannel.is_completion_sentinel_text(normalized_content):
                parts.append(Part(text=normalized_content, metadata=thought_metadata))
        elif content is not None and not isinstance(content, (dict, list)):
            parts.append(Part(text=str(content), metadata=thought_metadata))
        result = payload.get("result")
        if isinstance(result, str) and result.strip():
            normalized_result = result.strip()
            if not A2AChannel.is_completion_sentinel_text(normalized_result):
                parts.append(Part(text=normalized_result))
        # Surface tool events in stream mode so callers can observe progress.
        if msg.event_type == EventType.CHAT_TOOL_CALL and isinstance(payload.get("tool_call"), dict):
            tool_call = payload.get("tool_call") or {}
            tool_name = str(tool_call.get("name") or "tool").strip()
            parts.append(Part(text=f"[tool_call] {tool_name}"))
        if msg.event_type == EventType.CHAT_TOOL_RESULT:
            tool_name = str(payload.get("tool_name") or "").strip()
            tool_result = payload.get("result")
            if isinstance(tool_result, str) and tool_result.strip():
                label = f"[tool_result:{tool_name}] " if tool_name else "[tool_result] "
                parts.append(Part(text=f"{label}{tool_result.strip()}"))

        raw_files = payload.get("files")
        files = raw_files if isinstance(raw_files, list) else []
        for idx, file_item in enumerate(files):
            if not isinstance(file_item, dict):
                continue
            file_name = str(file_item.get("filename") or file_item.get("name") or f"file_{idx}").strip()
            media_type = str(file_item.get("media_type") or file_item.get("type") or "").strip()
            url = str(file_item.get("url") or file_item.get("uri") or "").strip()
            data = str(file_item.get("data") or "").strip()
            raw = str(file_item.get("raw") or "").strip()

            common_fields: dict[str, str] = {}
            if file_name:
                common_fields["filename"] = file_name
            if media_type:
                common_fields["media_type"] = media_type
            if url:
                parts.append(Part(url=url, **common_fields))
            if data:
                parts.append(Part(data=data, **common_fields))
            if raw:
                parts.append(Part(raw=raw, **common_fields))

        if parts:
            return parts
        if fallback_to_text:
            return [Part(text=A2AChannel.message_to_text(msg))]
        return []

    @staticmethod
    def is_completion_sentinel_text(text: str) -> bool:
        compact = "".join(text.split()).lower()
        return compact in {"{'is_complete':true}", '{"is_complete":true}'}

    @staticmethod
    def is_terminal_message(msg: Message) -> bool:
        if msg.type == "res":
            return True
        if msg.type != "event":
            return False
        if msg.event_type in {
            EventType.CHAT_ERROR,
            EventType.CHAT_INTERRUPT_RESULT,
        }:
            return True
        payload = msg.payload if isinstance(msg.payload, dict) else {}
        if payload.get("is_complete") is True:
            return True
        return False

    @staticmethod
    def _build_request_params(
        *,
        query: str,
        files: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"query": query}
        if files:
            params["files"] = files
        return params

    @staticmethod
    def map_a2a_parts_to_params(a2a_message: Any) -> tuple[str, list[dict[str, Any]]]:
        """Map A2A message parts to JiuwenSwarm-friendly query/files params."""
        if a2a_message is None:
            return "", []

        text_segments: list[str] = []
        files: list[dict[str, Any]] = []
        parts = getattr(a2a_message, "parts", None) or []
        for idx, part in enumerate(parts):
            text = str(getattr(part, "text", "") or "").strip()
            if text:
                text_segments.append(text)

            file_name = str(getattr(part, "filename", "") or "").strip()
            media_type = str(getattr(part, "media_type", "") or "").strip()
            url = str(getattr(part, "url", "") or "").strip()
            data = str(getattr(part, "data", "") or "").strip()
            raw = str(getattr(part, "raw", "") or "").strip()

            # Preserve non-text parts as files metadata for downstream tools.
            if url or data or raw:
                normalized_name = file_name or f"a2a_part_{idx}"
                entry: dict[str, Any] = {
                    # web_channel compatibility keys
                    "name": normalized_name,
                    "filename": normalized_name,
                }
                if media_type:
                    entry["media_type"] = media_type
                    # common consumers check `type`
                    entry["type"] = media_type
                if url:
                    entry["url"] = url
                    entry["uri"] = url
                if data:
                    entry["data"] = data
                    entry["encoding"] = "base64"
                if raw:
                    entry["raw"] = raw
                files.append(entry)

        query = "\n".join(seg for seg in text_segments if seg).strip()
        return query, files
