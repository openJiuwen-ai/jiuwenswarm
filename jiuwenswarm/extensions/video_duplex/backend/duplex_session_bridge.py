"""Authenticated browser media bridge with server-owned vLLM-Omni tool execution."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import websockets
from starlette.websockets import WebSocket, WebSocketDisconnect

from jiuwenswarm.runtime.tool_calls import ToolCall, ToolCallCoordinator, ToolExecutionContext
from .duplex_task_executor import JiuwenTaskExecutor
from .qwen_omni_tools import qwen_omni_tools
from .tool_adapters.vllm_omni import VllmOmniConfig, VllmOmniToolAdapter

if TYPE_CHECKING:
    from .task_adapter import VideoSearchManager

logger = logging.getLogger(__name__)
VLLM_OMNI_PROXY_PATH = "/ws/video/vllm-omni"


def tools() -> tuple[dict, ...]:
    return tuple({"type": "function", **item["function"]}
                 for item in qwen_omni_tools(queue_manager=True)
                 if item["function"]["name"] in {"jiuwen_delegate", "jiuwen_queue_manage"})


class DuplexBridgeRegistry:
    """Single-use tickets minted only by the authenticated config RPC."""

    def __init__(self, manager: VideoSearchManager) -> None:
        self.manager = manager
        self.tickets: dict = {}
        self.active: set[asyncio.Task] = set()

    async def issue(self, ws: WebSocket, scope: str, instructions: str = "") -> str:
        owner, scope = await self.manager.scope(ws, {"search_session_id": scope})
        now = time.monotonic()
        self.tickets = {key: value for key, value in self.tickets.items() if value[0] > now}
        if len(self.tickets) >= 128:
            raise ValueError("Too many pending duplex sessions")
        token = uuid.uuid4().hex

        async def credential_resolver(target: dict) -> str:
            from .task_adapter import task_login_credential_ref

            return await task_login_credential_ref(ws, target)

        self.tickets[token] = (now + 30, owner, scope, instructions, credential_resolver)
        return VLLM_OMNI_PROXY_PATH + "?ticket=" + token

    async def serve(self, websocket: WebSocket) -> None:
        await websocket.accept()
        ticket = self.tickets.pop(websocket.query_params.get("ticket", ""), None)
        if ticket is None or ticket[0] <= time.monotonic():
            await websocket.close(code=1008, reason="Invalid or expired duplex ticket")
            return
        task = asyncio.current_task()
        self.active.add(task)
        try:
            await serve_session(
                websocket, self.manager, ToolExecutionContext(ticket[1], ticket[2]), ticket[3],
                credential_resolver=ticket[4],
            )
        except Exception:
            logger.exception("Could not establish managed duplex session")
            try:
                await websocket.close(code=1011, reason="Duplex backend connection failed")
            except (RuntimeError, WebSocketDisconnect):
                pass
        finally:
            self.active.discard(task)

    async def close(self) -> None:
        self.tickets.clear()
        tasks = tuple(self.active)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def serve_session(
    websocket: WebSocket,
    manager: VideoSearchManager,
    context: ToolExecutionContext,
    instructions: str = "",
    *,
    credential_resolver: Callable[[dict], Awaitable[str]] | None = None,
) -> None:
    config = VllmOmniConfig.from_environment()
    endpoint = config.endpoint()
    headers = {"Authorization": "Bearer " + config.api_key} if config.api_key else {}
    async with websockets.connect(
        endpoint, additional_headers=headers, open_timeout=15, close_timeout=3, max_size=8 << 20,
    ) as upstream:
        lock = asyncio.Lock()

        async def send(event: dict) -> None:
            async with lock:
                async with asyncio.timeout(5):
                    await upstream.send(json.dumps(event, allow_nan=False))

        adapter = VllmOmniToolAdapter(send)
        latest_frame = ""

        def frame_lookup(_call: ToolCall) -> str:
            return latest_frame

        def map_media(event: dict) -> tuple[dict, ...]:
            nonlocal latest_frame
            commands = adapter.media_commands(event)
            frames = event.get("video_frames") or []
            if frames:
                latest_frame = frames[0]
            return commands

        executor = JiuwenTaskExecutor(manager, frame_lookup=frame_lookup, credential_resolver=credential_resolver)
        coordinator = ToolCallCoordinator(adapter, executor, context, timeout_s=config.timeout_s)
        ready = asyncio.Event()
        outgoing = asyncio.Queue(maxsize=128)

        async def browser_writer() -> None:
            while True:
                event = await outgoing.get()
                async with asyncio.timeout(5):
                    await websocket.send_text(json.dumps(event))

        async def upstream_reader() -> None:
            configured = False
            async for raw in upstream:
                event = json.loads(raw)
                kind = event.get("type")
                if kind == "session.created":
                    adapter.adopt(event.get("session") or {}, tool_count=len(tools()))
                    adapter.ready = configured = True
                    ready.set()
                    outgoing.put_nowait({"type": "duplex.ready", "session": event["session"]})
                elif kind == "session.updated" and not configured:
                    adapter.adopt(event.get("session") or {}, tool_count=len(tools()))
                    adapter.ready = configured = True
                    ready.set()
                    outgoing.put_nowait({"type": "duplex.ready", "session": event["session"]})
                elif kind == "error" and not configured:
                    raise ValueError("Backend rejected duplex configuration")
                elif kind in {"session.closed", "session.expired", "session.resync_required"}:
                    return
                for normalized in adapter.normalize(event):
                    await coordinator.handle(normalized)
                    if normalized.kind in {"response_done", "playback_done"}:
                        await adapter.flush_continuations()
                # The browser never owns protocol configuration, tool output or continuation.
                if configured and kind not in {"session.created", "session.updated"}:
                    outgoing.put_nowait(event)

        async def browser_reader() -> None:
            async with asyncio.timeout(30):
                await ready.wait()
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    return
                raw = message.get("text", "")
                if len(raw) > 8 << 20:
                    raise ValueError("Browser frame exceeds limit")
                event = json.loads(raw)
                kind = event.get("type")
                if kind == "duplex.stop":
                    return
                if kind == "duplex.user_text":
                    text = event.get("text")
                    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 16000:
                        raise ValueError("Invalid user text")
                    for command in map_media({
                        "type": "input_audio_buffer.append", "audio": "",
                        "video_frames": event.get("video_frames", []),
                    }):
                        if command["type"] != "input_audio_buffer.append" or command.get("video_frames"):
                            await send(command)
                    await send({"type": "conversation.item.create", "item": {
                        "type": "message", "role": "user",
                        "content": [{"type": "input_text", "text": text.strip()}],
                    }})
                    await send({"type": "response.create"})
                    continue
                if kind not in {"input_audio_buffer.append", "input_audio_buffer.commit", "playback.ack"}:
                    raise ValueError("Browser command is not allowed on a managed duplex session")
                if kind == "input_audio_buffer.append":
                    # Only media fields are accepted; client text never grants execution authority.
                    event = {key: event[key] for key in (
                        "type", "audio", "video_frames", "format", "sample_rate_hz", "is_speech",
                    ) if key in event}
                elif kind == "input_audio_buffer.commit":
                    event = {"type": kind, "final": True}
                for command in map_media(event):
                    await send(command)

        async def session_watcher() -> None:
            if not context.scope.startswith("task-duplex:"):
                await asyncio.Future()
            from jiuwenswarm.common.schema.message import ReqMethod
            from .task_adapter import task_agent_query

            session = context.scope.removeprefix("task-duplex:")
            while True:
                await asyncio.sleep(1)
                metadata = await task_agent_query(
                    manager.client, ReqMethod.SESSION_GET_METADATA,
                    {"session_id": session}, session, context.owner_id,
                )
                if (not metadata or metadata.get("archived")
                        or str(metadata.get("user_id") or "").strip() != context.owner_id):
                    return

        await adapter.configure(tools(), config, instructions)
        tasks = [asyncio.create_task(operation()) for operation in (
            browser_writer, upstream_reader, browser_reader, session_watcher,
        )]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except (WebSocketDisconnect, websockets.ConnectionClosed):
            pass
        except Exception:
            logger.exception("Managed duplex session failed")
            try:
                await websocket.send_text(json.dumps({
                    "type": "error", "error": {"code": "DUPLEX_SESSION_FAILED",
                    "message": "Duplex session failed. Check server configuration and diagnostics."},
                }))
            except (RuntimeError, WebSocketDisconnect):
                pass
        finally:
            await adapter.close()
            try:
                await send({"type": "session.close"})
            except Exception:
                pass
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await coordinator.close()
            try:
                await websocket.close(code=1000)
            except (RuntimeError, WebSocketDisconnect):
                pass
