# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Gateway-side AgentServerClient telemetry proxy."""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Awaitable, Callable
from urllib.parse import urlparse

from opentelemetry import context, trace
from opentelemetry.trace import SpanKind, StatusCode

from jiuwenclaw.e2a.models import E2AEnvelope
from jiuwenclaw.gateway.agent_client import AgentServerClient
from jiuwenclaw.schema.agent import AgentResponse, AgentResponseChunk
from jiuwenclaw.telemetry.context_propagation import inject_trace_context

_tracer = trace.get_tracer("jiuwenclaw.gateway.agent_client")
GATEWAY_CLIENT_INSTRUMENTED_ATTR = "_jiuwenclaw_gateway_client_instrumented"

_EXTENSION_PROXY_MARKER = "_jiuwenclaw_agent_client_extension_proxy"


class TelemetryAgentServerClientProxy(AgentServerClient):
    """Wrap an AgentServerClient and emit CLIENT spans around Gateway RPCs."""

    def __init__(self, inner: AgentServerClient, *, target_uri: str | None = None) -> None:
        self._inner = inner
        self._target_uri = target_uri

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def connect(self, uri: str) -> None:
        self._target_uri = uri or self._target_uri
        await self._inner.connect(uri)

    async def disconnect(self) -> None:
        await self._inner.disconnect()

    def set_or_update_server_config(self, *, config: Any, env: dict[str, str] | None = None) -> None:
        self._inner.set_or_update_server_config(config=config, env=env)

    async def send_request(self, envelope: E2AEnvelope) -> AgentResponse:
        return await _send_request_with_telemetry(
            self._inner.send_request,
            envelope=envelope,
            target_uri=self._target_uri or getattr(self._inner, "_uri", None),
        )

    async def send_request_stream(self, envelope: E2AEnvelope) -> AsyncIterator[AgentResponseChunk]:
        async for chunk in _send_request_stream_with_telemetry(
            self._inner.send_request_stream,
            envelope=envelope,
            target_uri=self._target_uri or getattr(self._inner, "_uri", None),
        ):
            yield chunk

    def get_target_uri(self) -> str | None:
        """Get the target URI for this client proxy."""
        return self._target_uri

    def set_target_uri(self, target_uri: str | None) -> None:
        """Set the target URI for this client proxy."""
        self._target_uri = target_uri


class TelemetryAgentServerClientExtensionProxy:
    """Wrap an AgentServerClient extension and lazily instrument returned clients."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self._cached_client: Any = None
        self._cached_wrapped_client: Any = None
        setattr(self, _EXTENSION_PROXY_MARKER, True)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def initialize(self, config: Any) -> None:
        await self._inner.initialize(config)

    async def shutdown(self) -> None:
        await self._inner.shutdown()

    def get_client(self) -> AgentServerClient:
        client = self._inner.get_client()
        if client is self._cached_client and self._cached_wrapped_client is not None:
            return self._cached_wrapped_client

        wrapped = wrap_agent_client(client)
        self._cached_client = client
        self._cached_wrapped_client = wrapped
        return wrapped


def wrap_agent_client(client: AgentServerClient, *, target_uri: str | None = None) -> AgentServerClient:
    """Return a telemetry proxy when telemetry has been initialized."""
    import jiuwenclaw.telemetry as telemetry_mod

    if not telemetry_mod.is_telemetry_initialized():
        return client
    if isinstance(client, TelemetryAgentServerClientProxy):
        if target_uri and not client.get_target_uri():
            client.set_target_uri(target_uri)
        return client
    if getattr(client.__class__, GATEWAY_CLIENT_INSTRUMENTED_ATTR, False):
        return client
    return TelemetryAgentServerClientProxy(client, target_uri=target_uri)


def wrap_agent_client_extension(extension: Any) -> Any:
    """Return an extension proxy that instruments the client returned by get_client()."""
    import jiuwenclaw.telemetry as telemetry_mod

    if not telemetry_mod.is_telemetry_initialized():
        return extension
    if getattr(extension, _EXTENSION_PROXY_MARKER, False):
        return extension
    return TelemetryAgentServerClientExtensionProxy(extension)


async def _send_request_with_telemetry(
    send_request: Callable[[E2AEnvelope], Awaitable[AgentResponse]],
    *,
    envelope: E2AEnvelope,
    target_uri: str | None,
) -> AgentResponse:
    with _tracer.start_as_current_span(
        "jiuwenclaw.gateway.agent.request",
        kind=SpanKind.CLIENT,
        attributes=_build_attrs(target_uri, envelope, False),
    ) as span:
        _inject_span_context(envelope)
        try:
            resp = await send_request(envelope)
            span.set_status(StatusCode.OK)
            return resp
        except asyncio.CancelledError:
            span.set_attribute("jiuwenclaw.cancelled", True)
            raise
        except Exception as exc:
            span.set_status(StatusCode.ERROR, str(exc)[:256])
            span.record_exception(exc)
            raise


async def _send_request_stream_with_telemetry(
    send_request_stream: Callable[[E2AEnvelope], AsyncIterator[AgentResponseChunk]],
    *,
    envelope: E2AEnvelope,
    target_uri: str | None,
) -> AsyncIterator[AgentResponseChunk]:
    envelope.is_stream = True
    span = _tracer.start_span(
        "jiuwenclaw.gateway.agent.request",
        kind=SpanKind.CLIENT,
        attributes=_build_attrs(target_uri, envelope, True),
    )
    ctx = trace.set_span_in_context(span)
    token = context.attach(ctx)
    try:
        _inject_span_context(envelope)
        async for chunk in send_request_stream(envelope):
            yield chunk
        span.set_status(StatusCode.OK)
    except asyncio.CancelledError:
        span.set_attribute("jiuwenclaw.cancelled", True)
        raise
    except Exception as exc:
        span.set_status(StatusCode.ERROR, str(exc)[:256])
        span.record_exception(exc)
        raise
    finally:
        span.end()
        context.detach(token)


def _inject_span_context(envelope: E2AEnvelope) -> None:
    carrier = envelope.channel_context if isinstance(envelope.channel_context, dict) else {}
    envelope.channel_context = carrier
    inject_trace_context(carrier)


def _build_attrs(target_uri: str | None, envelope: E2AEnvelope, is_stream: bool) -> dict[str, Any]:
    attrs: dict[str, Any] = {
        "jiuwenclaw.request.id": envelope.request_id or "",
        "jiuwenclaw.channel.id": envelope.channel or "",
        "jiuwenclaw.session.id": envelope.session_id or "",
        "jiuwenclaw.req.method": envelope.method or "",
        "jiuwenclaw.stream": bool(is_stream),
    }

    parsed = urlparse(target_uri) if target_uri else None
    if parsed and parsed.hostname:
        attrs["server.address"] = parsed.hostname
    if parsed and parsed.port is not None:
        attrs["server.port"] = parsed.port
    if parsed and parsed.scheme:
        attrs["network.protocol.name"] = "websocket" if parsed.scheme in ("ws", "wss") else parsed.scheme

    return attrs
