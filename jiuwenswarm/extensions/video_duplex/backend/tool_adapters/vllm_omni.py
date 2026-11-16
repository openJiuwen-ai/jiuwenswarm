"""vLLM-Omni PR #9909 Realtime tool protocol, without inference dependencies."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import math
import os
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from jiuwenswarm.runtime.tool_calls.types import ToolCall, ToolEvent, ToolResult


@dataclass(frozen=True)
class VllmOmniConfig:
    url: str
    model: str
    api_key: str = ""
    timeout_s: float = 120

    @classmethod
    def from_environment(cls) -> VllmOmniConfig:
        return cls(
            os.getenv("VLLM_OMNI_REALTIME_URL", "").strip(),
            os.getenv("VLLM_OMNI_MODEL_NAME", "").strip(),
            os.getenv("VLLM_OMNI_API_KEY", "").strip(),
            float(os.getenv("VLLM_OMNI_TOOL_TIMEOUT_S", "120")),
        )

    def endpoint(self) -> str:
        parts = urlsplit(self.url)
        if parts.scheme not in {"ws", "wss"} or not parts.hostname or parts.username or parts.password:
            raise ValueError("VLLM_OMNI_REALTIME_URL must be a ws/wss URL without credentials")
        if not self.model or not math.isfinite(self.timeout_s) or self.timeout_s <= 0:
            raise ValueError("A model and positive finite tool timeout are required")
        query = dict(parse_qsl(parts.query))
        query.update(model=self.model, duplex="1", autostart="0")
        return urlunsplit((parts.scheme, parts.netloc, parts.path or "/v1/realtime", urlencode(query), ""))


class VllmOmniToolAdapter:
    def __init__(self, send: Callable[[dict], Awaitable[None]], *, generation: str | None = None) -> None:
        self.send = send
        self.generation = generation or uuid.uuid4().hex
        self.session_id = ""
        self.capabilities: dict = {}
        self.ready = False
        self.closed = False
        self.responses: set[str] = set()
        self.playback: set[str] = set()
        self.pending: dict[str, tuple[str, asyncio.Future[bool], str]] = {}
        self.accepted: set[str] = set()
        self.abandoned: set[str] = set()
        self.continuations: set[str] = set()
        self.continued: set[str] = set()
        self._continuation_lock = asyncio.Lock()
        self._camera_item = ""
        self._camera_sequence = 0

    async def configure(self, tools: tuple[dict, ...], config: VllmOmniConfig, instructions: str = "") -> None:
        await self.send({
            "type": "session.update",
            "event_id": "configure-" + self.generation,
            "session": {
                "model": config.model,
                "modalities": ["audio", "text"],
                "input_audio_format": "pcm16",
                "output_audio_format": "pcm16",
                "sample_rate_hz": 16000,
                "audio": {"input": {"sample_rate_hz": 16000}, "output": {"sample_rate_hz": 24000}},
                "tools": list(tools),
                "tool_choice": "auto",
                "instructions": instructions,
                "extra_body": {
                    "tool_result_timeout_s": config.timeout_s,
                    "function_call_interrupt_policy": "keep",
                    "function_call_retry_policy": "user_turn",
                },
            },
        })

    def adopt(self, session: dict, *, tool_count: int) -> None:
        caps = session.get("capabilities") or {}
        if caps.get("tool_mode") != "declared":
            raise ValueError("Backend must support declared tools")
        if caps.get("function_call_output_continuation") not in {"automatic", "client_response_create"}:
            raise ValueError("Unsupported tool-result continuation")
        maximum = caps.get("max_tools", 0)
        if maximum is not None and (not isinstance(maximum, int) or tool_count > maximum):
            raise ValueError("Backend tool capacity is insufficient")
        session_id = session.get("id") or session.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("Backend did not identify its session")
        if self.session_id and session_id != self.session_id:
            raise ValueError("Backend session identity changed")
        self.session_id, self.capabilities = session_id, dict(caps)

    def normalize(self, event: Mapping[str, object]) -> tuple[ToolEvent, ...]:
        kind = event.get("type")
        if kind == "response.created":
            response = event.get("response") or {}
            self.responses.add(str(response.get("id") or event.get("response_id") or ""))
        elif kind == "response.done":
            response = event.get("response") or {}
            self.responses.discard(str(response.get("id") or event.get("response_id") or ""))
            return (ToolEvent("response_done"),)
        elif kind == "response.output_audio.delta" and event.get("delta"):
            response_id = event.get("response_id")
            if isinstance(response_id, str) and response_id:
                self.playback.add(response_id)
        elif kind == "playback.acknowledged":
            details = event.get("event") or {}
            cursor = details.get("playback") or {}
            item_id = details.get("item_id")
            sent, committed = cursor.get("sent_ms"), cursor.get("committed_ms")
            if (isinstance(item_id, str) and item_id.startswith("item_")
                    and isinstance(sent, int) and isinstance(committed, int) and committed >= sent):
                self.playback.discard(item_id.removeprefix("item_"))
                return (ToolEvent("playback_done"),)
        elif kind == "output_audio_buffer.cleared":
            self.playback.discard(str(event.get("response_id") or ""))
            return (ToolEvent("playback_done"),)
        elif kind in {"conversation.item.added", "conversation.item.created", "conversation.item.done"}:
            item = event.get("item") or {}
            call_id = item.get("call_id")
            pending = self.pending.get(call_id)
            if (item.get("type") == "function_call_output" and pending
                    and item.get("id") == pending[2] and event.get("created") is not False):
                # Realtime projection emits the committed item with our id;
                # the engine-internal `created` flag isn't a GA wire field.
                if not pending[1].done():
                    self.accepted.add(call_id)
                    pending[1].set_result(True)
        elif kind == "error":
            error = event.get("error") or event
            related = error.get("event_id")
            for call_id, (event_id, future, _item_id) in self.pending.items():
                if related == event_id and not future.done():
                    future.set_result(False)
        elif kind == "duplex.function_call.settled":
            details = event.get("event") or {}
            call_id = str(details.get("call_id") or "")
            self.abandoned.add(call_id)
            self.continuations.discard(call_id)
            pending = self.pending.get(call_id)
            if pending and not pending[1].done():
                pending[1].set_result(False)
            return (ToolEvent("settled", call_id),)
        elif kind in {"response.function_call_arguments.done", "response.output_item.done"}:
            item = event if kind == "response.function_call_arguments.done" else event.get("item") or {}
            if kind == "response.output_item.done" and item.get("type") != "function_call":
                return ()
            if kind == "response.function_call_arguments.done" and "name" not in item:
                # The standard arguments projection has no tool binding. The
                # complete output_item.done follows with its name and arguments.
                return ()
            call_id, name = item.get("call_id"), item.get("name")
            if not isinstance(call_id, str) or not call_id or len(call_id) > 200:
                raise ValueError("Invalid backend tool identity")
            if call_id in self.abandoned:
                return ()
            try:
                arguments = item.get("arguments", "{}")
                if isinstance(arguments, str):
                    arguments = json.loads(arguments)
                if not isinstance(arguments, dict) or not isinstance(name, str) or not name:
                    raise ValueError("Invalid tool name or arguments")
                json.dumps(arguments, allow_nan=False)
            except (ValueError, TypeError) as exc:
                raise ValueError("Invalid backend tool arguments") from exc
            if not self.ready:
                raise ValueError("Tool call arrived before configuration confirmation")
            call = ToolCall(self.session_id, self.generation, call_id, name, arguments)
            return (ToolEvent("call", call_id, call),)
        return ()

    def media_commands(self, event: dict) -> tuple[dict, ...]:
        """Map browser frames to the model's declared media carrier, without model-name branches."""
        frames = event.get("video_frames") or []
        if not isinstance(frames, list) or len(frames) > 1:
            raise ValueError("Only one current camera frame is allowed per append")
        if any(not isinstance(frame, str) or len(frame) > 4 << 20 for frame in frames):
            raise ValueError("Invalid camera frame")
        for frame in frames:
            prefix, separator, encoded = frame.partition(",")
            if not separator or prefix not in {
                "data:image/jpeg;base64", "data:image/png;base64", "data:image/webp;base64",
            }:
                raise ValueError("Camera frames must be inline image data")
            try:
                if not base64.b64decode(encoded, validate=True):
                    raise ValueError("Empty camera frame")
            except binascii.Error as exc:
                raise ValueError("Invalid camera frame encoding") from exc
        if frames and "video" not in self.capabilities.get("optional_input_modalities", []):
            audio = {key: value for key, value in event.items() if key != "video_frames"}
            if not self.capabilities.get("supports_image_input"):
                return (audio,)
            commands = []
            if self._camera_item:
                commands.append({"type": "conversation.item.delete", "item_id": self._camera_item})
            self._camera_sequence += 1
            self._camera_item = f"camera-{self.generation}-{self._camera_sequence}"
            commands.append({"type": "conversation.item.create", "item": {
                "id": self._camera_item, "type": "message", "role": "user",
                "content": [{"type": "input_image", "image_url": frames[0]}],
            }})
            return (*commands, audio)
        if frames:
            event = {**event, "video_frames": [frame.split(",", 1)[-1] for frame in frames]}
        return (event,)

    async def submit_result(self, call: ToolCall, result: ToolResult) -> bool:
        if self.closed or call.call_id in self.abandoned:
            return False
        event_id = "result-" + call.command_id
        item_id = "item-tool-" + call.command_id.removeprefix("duplex:")
        future = asyncio.get_running_loop().create_future()
        self.pending[call.call_id] = (event_id, future, item_id)
        try:
            await self.send({
                "type": "conversation.item.create", "event_id": event_id,
                "item": {"id": item_id, "type": "function_call_output", "call_id": call.call_id,
                         "output": result.content},
            })
            return await future
        finally:
            self.pending.pop(call.call_id, None)

    async def cancel_call(self, call: ToolCall, reason: str) -> None:
        self.continuations.discard(call.call_id)
        if self.closed or call.call_id in self.abandoned or call.call_id in self.accepted:
            return
        self.abandoned.add(call.call_id)
        await self.send({"type": "function_call.cancel", "call_id": call.call_id, "reason": reason})

    async def continue_after_result(self, call: ToolCall) -> None:
        if (call.call_id in self.accepted and call.call_id not in self.continued
                and self.capabilities.get("function_call_output_continuation") == "client_response_create"):
            self.continued.add(call.call_id)
            self.continuations.add(call.call_id)
            await self.flush_continuations()

    async def flush_continuations(self) -> None:
        async with self._continuation_lock:
            if self.closed or self.responses or self.playback:
                return
            eligible = self.continuations - self.abandoned
            self.continuations.clear()
            if eligible:
                await self.send({"type": "response.create"})

    async def close(self) -> None:
        self.closed = True
        self.continuations.clear()
        for _, future, _item_id in self.pending.values():
            if not future.done():
                future.set_result(False)
