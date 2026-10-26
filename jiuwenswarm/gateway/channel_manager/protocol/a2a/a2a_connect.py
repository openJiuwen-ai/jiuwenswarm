from __future__ import annotations

import asyncio
import inspect
import logging
import os
import socket
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from jiuwenswarm.gateway.channel_manager.base import BaseChannel
from jiuwenswarm.common.e2a.acp.acp_tool_updates import is_reasoning_event
from jiuwenswarm.common.schema.message import EventType, Message, ReqMethod
from jiuwenswarm.gateway.routing.session_sharing import RoutingTarget
from .security import (
    A2AAuthenticationMiddleware,
    agent_card_security,
    validate_security,
)

logger = logging.getLogger(__name__)

try:
    from a2a.server.agent_execution import AgentExecutor as _AgentExecutorBase
except ImportError:
    _AgentExecutorBase = object

# Part-level metadata key marking reasoning/thought content, mirroring the
# convention popularized by Google ADK (`adk_thought`). A2A itself has no
# first-class thought field; `Part.metadata` is the official extension point.
A2A_THOUGHT_METADATA_KEY = "jiuwen_thought"

# Only inert observability fields may cross the untrusted A2A ingress boundary.
# Gateway control metadata (cwd, user_id, ws_id, member_name, memory flags, ...)
# is deliberately never accepted from an A2A caller.
_A2A_INGRESS_METADATA_ALLOWLIST = frozenset(
    {"correlation_id", "trace_id", "traceparent", "tracestate"}
)
_A2A_INGRESS_METADATA_VALUE_MAX_LENGTH = 512
_A2A_SERVER_START_TIMEOUT_SECONDS = 10.0
_A2A_SERVER_START_POLL_SECONDS = 0.01


class A2ADependencyMissingError(RuntimeError):
    """Raised when the optional A2A SDK extra is unavailable."""


def _raise_missing_a2a_sdk(exc: ImportError) -> None:
    raise A2ADependencyMissingError(
        "A2A server is enabled but optional dependency `a2a-sdk[http-server]>=1.0.0` "
        'is not installed. Install with `pip install -e ".[a2a]"` or `uv sync --extra a2a`.'
    ) from exc


async def _enqueue_event_safely(event_queue: Any, event: Any) -> None:
    """Best-effort enqueue; cancel may already have closed the A2A queue."""
    try:
        await event_queue.enqueue_event(event)
    except Exception:  # noqa: BLE001
        logger.debug(
            "[A2AChannel] skipped event because the A2A event queue is unavailable",
            exc_info=True,
        )


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
    auth_type: str = "none"
    api_key_header: str = "X-API-Key"
    card_auth_required: bool = False
    credential_hash: str = field(default="", repr=False)


@dataclass
class _PendingA2ARequest:
    queue: asyncio.Queue[Message]
    session_id: str


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
        message_id = str(getattr(context.message, "message_id", "") or "") or None
        started_at = time.time()
        self._channel.notify_request_observer(
            request_id=request_id,
            context_id=context_id,
            message_id=message_id,
            status="processing",
            started_at=started_at,
        )

        try:
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
                self._channel.notify_request_observer(
                    request_id=request_id,
                    context_id=context_id,
                    message_id=message_id,
                    status="failed",
                    started_at=started_at,
                    finished_at=time.time(),
                    error="empty query",
                )
                return

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
                metadata=self._channel.filter_ingress_metadata(context.metadata),
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
                        history_status = "failed"
                        history_error = "agent request failed"
                    elif response_msg.event_type == EventType.CHAT_INTERRUPT_RESULT:
                        final_state = TaskState.TASK_STATE_CANCELED
                        history_status = "canceled"
                        history_error = None
                    else:
                        final_state = TaskState.TASK_STATE_COMPLETED
                        history_status = "completed"
                        history_error = None
                    await _enqueue_event_safely(
                        event_queue,
                        TaskStatusUpdateEvent(
                            task_id=task_id,
                            context_id=context_id,
                            status=TaskStatus(state=final_state),
                        ),
                    )
                    self._channel.notify_request_observer(
                        request_id=request_id,
                        context_id=context_id,
                        message_id=message_id,
                        status=history_status,
                        started_at=started_at,
                        finished_at=time.time(),
                        error=history_error,
                    )
                    break
        except asyncio.CancelledError:
            self._channel.notify_request_observer(
                request_id=request_id,
                context_id=context_id,
                message_id=message_id,
                status="canceled",
                started_at=started_at,
                finished_at=time.time(),
                error=None,
            )
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "[A2AChannel] execution failed: request_id=%s err=%s", request_id, exc
            )
            self._channel.notify_request_observer(
                request_id=request_id,
                context_id=context_id,
                message_id=message_id,
                status="failed",
                started_at=started_at,
                finished_at=time.time(),
                error="request execution failed",
            )
            await _enqueue_event_safely(
                event_queue,
                TaskStatusUpdateEvent(
                    task_id=task_id,
                    context_id=context_id,
                    status=TaskStatus(state=TaskState.TASK_STATE_FAILED),
                ),
            )
        finally:
            self._channel.clear_pending_request(request_id)
            await event_queue.close()

    async def cancel(self, context: Any, event_queue: Any) -> None:
        from a2a.types import TaskState, TaskStatus, TaskStatusUpdateEvent

        task = context.current_task or self._resolve_task(context)
        task_id = str(context.task_id or task.id)
        context_id = str(context.context_id or task.context_id)
        now = time.time()
        await self._channel.cancel_pending_request(task_id)
        self._channel.notify_request_observer(
            request_id=task_id,
            context_id=context_id,
            message_id=str(getattr(context.message, "message_id", "") or "") or None,
            status="canceled",
            finished_at=now,
        )
        await event_queue.enqueue_event(task)
        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=task_id,
                context_id=context_id,
                status=TaskStatus(state=TaskState.TASK_STATE_CANCELED),
            )
        )
        await event_queue.close()


class A2AChannel(BaseChannel):
    name = "a2a"

    def __init__(self, config: A2AChannelConfig, router: Any):
        super().__init__(config, router)
        self.config = config
        self._on_message_cb = None
        self._request_observer = None
        self._pending: dict[str, _PendingA2ARequest] = {}
        self._uvicorn_server: Any | None = None
        self._server_task: asyncio.Task | None = None
        self._listen_socket: socket.socket | None = None
        self._runtime_error_observer = None
        self._runtime_error_tasks: set[asyncio.Future[Any]] = set()
        self._stopping = False

    @property
    def channel_id(self) -> str:
        return str(self.config.channel_id or self.name).strip() or self.name

    def on_message(self, callback) -> None:
        self._on_message_cb = callback

    def set_request_observer(self, callback) -> None:
        """Observe request lifecycle metadata without retaining request bodies."""
        self._request_observer = callback

    def set_runtime_error_observer(self, callback) -> None:
        """Observe an unexpected server exit after the listen socket is ready."""
        self._runtime_error_observer = callback

    def notify_request_observer(self, **event: Any) -> None:
        if self._request_observer is None:
            return
        try:
            self._request_observer(dict(event))
        except Exception:  # noqa: BLE001
            logger.exception("[A2AChannel] request observer failed")

    async def start(self) -> None:
        if self._running:
            return
        if not self.config.enabled:
            logger.info("a2a.ingress disabled by config")
            return

        try:
            from a2a.server.request_handlers import DefaultRequestHandler
            from a2a.server.routes import (
                create_agent_card_routes,
                create_jsonrpc_routes,
            )
            from a2a.server.tasks import (
                InMemoryPushNotificationConfigStore,
                InMemoryTaskStore,
            )
            from a2a.types import (
                AgentCapabilities,
                AgentCard,
                AgentInterface,
                AgentSkill,
            )
            from fastapi import FastAPI
        except ImportError as exc:
            _raise_missing_a2a_sdk(exc)
        import uvicorn

        validate_security(self.config)
        agent_card = AgentCard(
            **agent_card_security(self.config),
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
            capabilities=AgentCapabilities(
                streaming=True,
                push_notifications=False,
            ),
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
        if self.config.extended_card_path != self.config.card_path:
            routes.extend(
                create_agent_card_routes(
                    agent_card,
                    card_url=self.config.extended_card_path,
                )
            )
        fastapi_app = FastAPI(
            routes=routes, docs_url=None, redoc_url=None, openapi_url=None
        )
        fastapi_app.add_middleware(A2AAuthenticationMiddleware, config=self.config)

        uv_cfg = uvicorn.Config(
            app=fastapi_app,
            host=self.config.host,
            port=self.config.port,
            log_level="info",
            access_log=False,
        )
        self._uvicorn_server = uvicorn.Server(uv_cfg)
        self._stopping = False
        # getaddrinfo() (DNS resolution) inside _bind_listen_socket is blocking;
        # run it off the event loop so a slow lookup doesn't stall other coroutines.
        listen_socket = await asyncio.to_thread(
            self._bind_listen_socket, self.config.host, self.config.port
        )
        self._listen_socket = listen_socket
        server_task = asyncio.create_task(
            self._uvicorn_server.serve(sockets=[listen_socket]),
            name="a2a-channel-server",
        )
        self._server_task = server_task
        try:
            async with asyncio.timeout(_A2A_SERVER_START_TIMEOUT_SECONDS):
                while not bool(getattr(self._uvicorn_server, "started", False)):
                    if server_task.done():
                        server_task.result()
                        raise RuntimeError(
                            "A2A server stopped before binding its listen socket"
                        )
                    await asyncio.sleep(_A2A_SERVER_START_POLL_SECONDS)
        except BaseException:
            self._uvicorn_server.should_exit = True
            if not server_task.done():
                server_task.cancel()
            try:
                await server_task
            # The original startup exception is the actionable one.
            except BaseException:
                pass
            listen_socket.close()
            self._listen_socket = None
            self._uvicorn_server = None
            self._server_task = None
            raise
        self._running = True
        server_task.add_done_callback(self._on_server_done)
        logger.info(
            "a2a.ingress started: http://%s:%s%s",
            self.config.host,
            self.config.port,
            self.config.rpc_path,
        )

    async def stop(self) -> None:
        self._stopping = True
        self._running = False
        try:
            pending_ids = list(self._pending)
            if pending_ids:
                # Cancel concurrently: sequential awaits could each block up to the
                # per-request ack timeout, making stop() take up to N x timeout.
                results = await asyncio.gather(
                    *(
                        self.cancel_pending_request(request_id)
                        for request_id in pending_ids
                    ),
                    return_exceptions=True,
                )
                for request_id, result in zip(pending_ids, results):
                    if isinstance(result, Exception):
                        logger.error(
                            "[A2AChannel] failed to cancel downstream request during shutdown: %s",
                            request_id,
                            exc_info=result,
                        )
            self._pending.clear()
            if self._uvicorn_server is not None:
                self._uvicorn_server.should_exit = True
            if self._server_task is not None:
                try:
                    await self._server_task
                except Exception as exc:  # noqa: BLE001
                    logger.warning("a2a.ingress shutdown with error: %s", exc)
        finally:
            self._uvicorn_server = None
            self._server_task = None
            if self._listen_socket is not None:
                self._listen_socket.close()
                self._listen_socket = None
            self._stopping = False
        logger.info("a2a.ingress stopped")

    @staticmethod
    def _bind_listen_socket(host: str, port: int) -> socket.socket:
        """Bind before reporting startup so address conflicts fail synchronously."""
        last_error: OSError | None = None
        for family, socktype, proto, _, address in socket.getaddrinfo(
            host,
            port,
            type=socket.SOCK_STREAM,
            flags=socket.AI_PASSIVE,
        ):
            listen_socket = socket.socket(family, socktype, proto)
            try:
                if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    listen_socket.setsockopt(
                        socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1
                    )
                else:
                    listen_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listen_socket.bind(address)
                listen_socket.listen(2048)
                listen_socket.setblocking(False)
                return listen_socket
            except OSError as exc:
                last_error = exc
                listen_socket.close()
        if last_error is not None:
            raise last_error
        raise OSError(f"Unable to resolve A2A listen address: {host}:{port}")

    def _on_server_done(self, task: asyncio.Task) -> None:
        if self._stopping or not self._running:
            return
        self._running = False
        try:
            error = task.exception()
        except asyncio.CancelledError:
            error = RuntimeError("A2A server task was canceled unexpectedly")
        if error is None:
            error = RuntimeError("A2A server stopped unexpectedly")
        logger.error("a2a.ingress server exited unexpectedly: %s", error)
        if self._runtime_error_observer is None:
            return
        try:
            result = self._runtime_error_observer(error)
            if inspect.isawaitable(result):
                observer_task = asyncio.ensure_future(result)
                if isinstance(observer_task, asyncio.Task):
                    observer_task.set_name("a2a-channel-runtime-error")
                self._runtime_error_tasks.add(observer_task)
                observer_task.add_done_callback(self._runtime_error_tasks.discard)
        except Exception:  # noqa: BLE001
            logger.exception("[A2AChannel] runtime error observer failed")

    async def send(
        self, msg: Message, *, routing_target: RoutingTarget | None = None
    ) -> None:
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
        normalized_session_id = session_id or f"a2a_{uuid.uuid4().hex[:8]}"
        pending = _PendingA2ARequest(
            queue=asyncio.Queue(),
            session_id=normalized_session_id,
        )
        self._pending[request_id] = pending
        try:
            msg = Message(
                id=request_id,
                type="req",
                channel_id=self.channel_id,
                session_id=normalized_session_id,
                params=self._build_request_params(query=query, files=files),
                timestamp=time.time(),
                ok=True,
                req_method=ReqMethod.CHAT_SEND,
                is_stream=True,
                metadata=dict(metadata or {}),
            )
            await self._dispatch_message(msg)
            return pending
        finally:
            # Keep pending entry for send() until terminal message is consumed by executor.
            pass

    def clear_pending_request(self, request_id: str) -> None:
        self._pending.pop(str(request_id), None)

    async def cancel_pending_request(self, request_id: str) -> bool:
        """Cancel the mapped Agent session, then wake the local A2A executor."""
        pending = self._pending.get(str(request_id))
        if pending is None:
            return False
        cancel_error: Exception | None = None
        try:
            await self._dispatch_message(
                Message(
                    id=f"{request_id}_cancel",
                    type="req",
                    channel_id=self.channel_id,
                    session_id=pending.session_id,
                    params={"intent": "cancel", "session_id": pending.session_id},
                    timestamp=time.time(),
                    ok=True,
                    req_method=ReqMethod.CHAT_CANCEL,
                    is_stream=False,
                    metadata={},
                )
            )
        except Exception as exc:  # noqa: BLE001
            cancel_error = exc
        await pending.queue.put(
            Message(
                id=str(request_id),
                type="event",
                channel_id=self.channel_id,
                session_id=None,
                params={},
                timestamp=time.time(),
                ok=False,
                payload={"is_complete": True},
                event_type=EventType.CHAT_INTERRUPT_RESULT,
            )
        )
        if cancel_error is not None:
            raise cancel_error
        return True

    async def _dispatch_message(self, msg: Message) -> None:
        if self._on_message_cb is None:
            raise RuntimeError("A2AChannel on_message callback is not set")
        result = self._on_message_cb(msg)
        if inspect.isawaitable(result):
            await result

    @staticmethod
    def filter_ingress_metadata(metadata: Any) -> dict[str, str]:
        """Copy only bounded, inert tracing metadata from an A2A caller."""
        if not isinstance(metadata, dict):
            return {}
        filtered: dict[str, str] = {}
        for key in _A2A_INGRESS_METADATA_ALLOWLIST:
            value = metadata.get(key)
            if isinstance(value, str) and value:
                filtered[key] = value[:_A2A_INGRESS_METADATA_VALUE_MAX_LENGTH]
        return filtered

    @staticmethod
    def message_to_text(msg: Message) -> str:
        payload = msg.payload if isinstance(msg.payload, dict) else {}
        if msg.type == "event" and msg.event_type == EventType.CHAT_ERROR:
            return str(
                payload.get("error") or payload.get("content") or "agent request failed"
            )
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
    def message_to_a2a_parts(
        msg: Message, *, fallback_to_text: bool = True
    ) -> list[Any]:
        """Map internal message payload to A2A response parts."""
        from a2a.types import Part

        payload = msg.payload if isinstance(msg.payload, dict) else {}
        parts: list[Any] = []

        # Keep error response readable for A2A callers.
        if msg.type == "event" and msg.event_type == EventType.CHAT_ERROR:
            error_text = str(
                payload.get("error") or payload.get("content") or "agent request failed"
            )
            return [Part(text=error_text)]

        thought_metadata = (
            {A2A_THOUGHT_METADATA_KEY: True}
            if A2AChannel.is_reasoning_message(msg)
            else None
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
        if msg.event_type == EventType.CHAT_TOOL_CALL and isinstance(
            payload.get("tool_call"), dict
        ):
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
            file_name = str(
                file_item.get("filename") or file_item.get("name") or f"file_{idx}"
            ).strip()
            media_type = str(
                file_item.get("media_type") or file_item.get("type") or ""
            ).strip()
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
        """Map text parts only.

        Breaking change vs. earlier batches: file/binary parts (url/data/raw) from
        inbound A2A messages are intentionally dropped rather than forwarded as
        `files`, since accepting arbitrary URLs/base64 blobs from external A2A
        callers is outside the current ingress security scope. Always returns
        ``files=[]``; see test_map_a2a_parts_to_params_forwards_text_but_drops_files.
        """
        if a2a_message is None:
            return "", []

        text_segments: list[str] = []
        parts = getattr(a2a_message, "parts", None) or []
        for part in parts:
            text = str(getattr(part, "text", "") or "").strip()
            if text:
                text_segments.append(text)

        query = "\n".join(seg for seg in text_segments if seg).strip()
        return query, []
