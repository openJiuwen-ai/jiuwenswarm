# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Injected execution services; concrete wire and extension code loads on use."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from jiuwenswarm.runtime.hooks import ExtensionRequestHooks


class TransportClosed(Exception):
    """A host sink closed before a response could be delivered."""


@dataclass(frozen=True, slots=True)
class HostCapabilities:
    """Optional execution facilities supplied by the composition root."""

    keepalive: bool = True
    before_chat_hook: bool = True
    diagnostics: bool = True


class HookDispatcher(Protocol):
    """Dispatch request hooks without exposing an extension registry to execution."""

    async def before_chat(self, request: Any) -> None:
        """Apply the host's before-chat policy to the mutable request."""


class HostServices(Protocol):
    """Delivery and integration boundary consumed by the resident execution host."""

    capabilities: HostCapabilities

    def encode_response(
        self, response: Any, *, response_id: str, sequence: int = 0
    ) -> dict[str, Any]:
        """Encode a terminal response using the host's delivery contract."""

    def encode_chunk(
        self, chunk: Any, *, response_id: str, sequence: int, is_stream: bool = True
    ) -> dict[str, Any]:
        """Encode one response chunk without consuming execution sequence numbers."""

    async def send(self, sink: Any, payload: dict[str, Any]) -> bool:
        """Deliver a payload, returning False when a size fallback was sent.

        Closed sinks raise TransportClosed; cancellation propagates unchanged.
        """

    def push_payload(self, message: dict[str, Any]) -> dict[str, Any]:
        """Encode a resident push event."""

    def is_transport_closed(self, exc: BaseException) -> bool:
        """Recognize closure from integrations outside the delivery wrapper."""

    def diagnostics(
        self, sink: Any, exc: BaseException, context: dict[str, Any]
    ) -> str:
        """Describe a delivery failure without exposing concrete connection APIs."""

    def error_payload(
        self, request: Any, error: str, code: str | None = None
    ) -> dict[str, Any]:
        """Encode a failed request using the same response contract."""

    async def before_chat(self, request: Any) -> None:
        """Invoke the injected before-chat hook when that capability is enabled."""


class ExtensionHookDispatcher(ExtensionRequestHooks):
    """Default hook bridge, importing extension machinery only on invocation."""


class ProductionHostServices:
    """Existing bounded E2A wire behavior behind a lazy integration boundary."""

    def __init__(
        self,
        *,
        hooks: HookDispatcher | None = None,
        capabilities: HostCapabilities | None = None,
    ) -> None:
        self.capabilities = (
            capabilities if capabilities is not None else HostCapabilities()
        )
        self._hooks = hooks if hooks is not None else ExtensionHookDispatcher()

    def encode_response(
        self, response: Any, *, response_id: str, sequence: int = 0
    ) -> dict[str, Any]:
        from jiuwenswarm.common.e2a.wire_codec import encode_agent_response_for_wire

        return encode_agent_response_for_wire(
            response, response_id=response_id, sequence=sequence
        )

    def encode_chunk(
        self, chunk: Any, *, response_id: str, sequence: int, is_stream: bool = True
    ) -> dict[str, Any]:
        from jiuwenswarm.common.e2a.wire_codec import encode_agent_chunk_for_wire

        return encode_agent_chunk_for_wire(
            chunk, response_id=response_id, sequence=sequence, is_stream=is_stream
        )

    async def send(self, sink: Any, payload: dict[str, Any]) -> bool:
        from websockets.exceptions import ConnectionClosed

        from jiuwenswarm.server.ws_send import send_wire_payload

        try:
            return await send_wire_payload(sink, payload)
        except ConnectionClosed as exc:
            raise TransportClosed(str(exc)) from exc

    def push_payload(self, message: dict[str, Any]) -> dict[str, Any]:
        from jiuwenswarm.server.gateway_push.wire import build_server_push_wire

        return build_server_push_wire(message)

    def is_transport_closed(self, exc: BaseException) -> bool:
        if isinstance(exc, TransportClosed):
            return True
        from websockets.exceptions import ConnectionClosed

        return isinstance(exc, ConnectionClosed)

    def diagnostics(
        self, sink: Any, exc: BaseException, context: dict[str, Any]
    ) -> str:
        if not self.capabilities.diagnostics:
            return str(exc)
        from gateway.common.ws_diagnostics import (
            describe_ws_exception,
            describe_ws_peer,
            format_ws_diagnostics,
        )

        original = (
            exc.__cause__
            if isinstance(exc, TransportClosed) and exc.__cause__ is not None
            else exc
        )
        return format_ws_diagnostics(
            context, describe_ws_peer(sink), describe_ws_exception(original)
        )

    def error_payload(
        self, request: Any, error: str, code: str | None = None
    ) -> dict[str, Any]:
        from jiuwenswarm.common.schema.agent import AgentResponse

        payload: dict[str, Any] = {"error": error}
        if code:
            payload["code"] = code
        response = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload=payload,
            metadata=request.metadata,
        )
        return self.encode_response(response, response_id=request.request_id)

    async def before_chat(self, request: Any) -> None:
        if self.capabilities.before_chat_hook:
            await self._hooks.before_chat(request)
