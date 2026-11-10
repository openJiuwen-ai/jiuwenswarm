"""Bind meeting controls to the extracted Gateway's application-plugin surface."""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from secrets import token_hex
from typing import Any

from agent_gateway.common.protocol_ids import is_valid_session_id
from agent_gateway.routing.e2a_proxy import fetch_agent_unary, proxy_unary_request
from openjiuwen_protocol.e2a.wire_codec import parse_agent_server_wire_chunk

from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.common.voice.input_stream import close_voice_input

from . import gateway_playback
from .playback import EVENT

logger = logging.getLogger(__name__)


class MeetingGatewayBinding:
    """Own browser routes and drain their media before releasing Team ownership."""

    def __init__(self, channel: Any, agent_client: Any) -> None:
        self.channel = channel
        self.agent_client = agent_client
        self._connections: set[Any] = set()
        self._push_callback_attribute: str | None = None
        self._previous_push_callback = None
        self._push_lock = asyncio.Lock()

    def bind(self) -> None:
        for method in (
            ReqMethod.MEETING_START, ReqMethod.MEETING_STATUS, ReqMethod.MEETING_END,
            ReqMethod.MEETING_RELEASE, ReqMethod.MEETING_INPUT_PREPARE, ReqMethod.MEETING_INPUT_ADMITTED,
        ):
            self.channel.register_method(method.value, partial(self.control, method), local_only=True)
        self.channel.register_method("meeting.playback.ready", self.playback_ready, local_only=True)
        for method, handler in (
            ("meeting.speech.synthesize", gateway_playback.synthesize_speech),
            ("meeting.playback.result", gateway_playback.playback_result),
            ("meeting.playback.stopped", gateway_playback.playback_stopped),
        ):
            self.channel.register_method(method, partial(handler, self.channel), local_only=True)
        self.channel.on_disconnect(self.disconnect)

    def _bind_playback_push(self) -> None:
        """Compose with the client's registered handler without changing Gateway."""
        setter = getattr(self.agent_client, "set_server_push_handler", None)
        # Gateway's WebSocket/runtime clients and routed clients store their
        # registered callback under these names; its public setter replaces it.
        for attribute in ("_on_server_push", "_push_handler"):
            previous = getattr(self.agent_client, attribute, None)
            if previous == self._handle_server_push:
                return
            if callable(setter) and callable(previous):
                self._push_callback_attribute = attribute
                self._previous_push_callback = previous
                setter(self._handle_server_push)
                return
        raise RuntimeError("Meeting playback requires an initialized Gateway push handler")

    async def _handle_server_push(self, wire: dict) -> None:
        try:
            chunk = parse_agent_server_wire_chunk(wire)
        except Exception:
            # Keep the Gateway's existing handling and diagnostics for invalid frames.
            await self._previous_push_callback(wire)
            return
        if isinstance(chunk.payload, dict) and chunk.payload.get("event_type") == EVENT:
            async with self._push_lock:
                await gateway_playback.handle_playback_push(chunk.payload)
            return
        await self._previous_push_callback(wire)

    async def control(self, method: ReqMethod, ws: Any, req_id: Any, params: Any, session_id: Any) -> None:
        params = params if isinstance(params, dict) else {}
        target = str(params.get("session_id") or session_id or "").strip()
        if not is_valid_session_id(target) or target == "new":
            await self.channel.send_response(
                ws, req_id, ok=False, error="Meeting session is required", code="BAD_REQUEST",
            )
            return
        owner_id = getattr(ws, "_meeting_owner_id", None)
        if method != ReqMethod.MEETING_STATUS and owner_id is None:
            owner_id = token_hex(16)
            setattr(ws, "_meeting_owner_id", owner_id)
        startup_id = str(params.get("startup_id") or "")
        owned = getattr(ws, "_meeting_owned_sessions", None)
        if owned is None:
            owned = {}
            setattr(ws, "_meeting_owned_sessions", owned)
        ownership_key = (target, startup_id)
        self._connections.add(ws)
        if method == ReqMethod.MEETING_START:
            owned[ownership_key] = None

        async def on_done(ok: bool, payload: dict) -> None:
            if method == ReqMethod.MEETING_START:
                if not ok:
                    owned.pop(ownership_key, None)
                elif getattr(ws, "_meeting_disconnected", False):
                    await self.release(ws, target, startup_id)
            elif ok and method == ReqMethod.MEETING_RELEASE:
                owned.pop(ownership_key, None)
                await gateway_playback.forget_playback_routes(ws, target, startup_id=startup_id)

        await proxy_unary_request(
            channel=self.channel, agent_client=self.agent_client, ws=ws, req_id=req_id,
            params=params, session_id=target, user_id=None, req_method=method, label=method.value,
            drop_params=("_meeting_owner_id", "_meeting_transport_loss"),
            extra_params={"_meeting_owner_id": owner_id} if owner_id else None, on_done=on_done,
        )

    async def playback_ready(self, ws: Any, req_id: Any, params: Any, session_id: Any) -> None:
        self._bind_playback_push()
        await gateway_playback.playback_ready(
            self.channel, lambda: self.agent_client, ws, req_id, params, session_id,
        )

    async def release(self, ws: Any, session_id: str, startup_id: str) -> None:
        owner_id = getattr(ws, "_meeting_owner_id", None)
        if not owner_id:
            return
        await close_voice_input(ws)
        transport_loss = await gateway_playback.drain_lost_playback(ws, session_id, startup_id)
        params = {"_meeting_owner_id": owner_id, "startup_id": startup_id}
        if transport_loss is not None:
            params["_meeting_transport_loss"] = transport_loss
        ok, payload = await fetch_agent_unary(
            agent_client=self.agent_client, req_method=ReqMethod.MEETING_RELEASE, params=params,
            session_id=session_id, user_id=None, channel_id=self.channel.channel_id,
            label="meeting.disconnect", timeout_seconds=5,
        )
        if not ok:
            raise RuntimeError(payload.get("error") or "Meeting disconnect release failed")
        await gateway_playback.forget_playback_routes(ws, session_id, startup_id=startup_id)
        getattr(ws, "_meeting_owned_sessions", {}).pop((session_id, startup_id), None)

    async def disconnect(self, ws: Any, session_ids: Any) -> None:
        setattr(ws, "_meeting_disconnected", True)
        owned = getattr(ws, "_meeting_owned_sessions", {})
        for owned_session, startup_id in list(owned):
            for attempt in range(3):
                try:
                    await self.release(ws, owned_session, startup_id)
                    break
                except Exception:
                    logger.exception("Meeting disconnect cleanup failed: session_id=%s attempt=%s", owned_session, attempt + 1)
                    if attempt < 2:
                        await asyncio.sleep(0.2 * (attempt + 1))
        self._connections.discard(ws)

    async def aclose(self) -> None:
        await asyncio.gather(*(self.disconnect(ws, ()) for ws in tuple(self._connections)))
        if (
            self._push_callback_attribute is not None
            and getattr(self.agent_client, self._push_callback_attribute, None) == self._handle_server_push
        ):
            self.agent_client.set_server_push_handler(self._previous_push_callback)
