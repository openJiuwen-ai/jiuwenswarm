"""Runtime-aware Gateway AgentServer client telemetry proxy."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from opentelemetry import context as otel_context, trace
from opentelemetry.trace import Span, SpanKind, StatusCode
from opentelemetry.util.types import AttributeValue

from jiuwenswarm.common.e2a.models import E2AEnvelope
from jiuwenswarm.common.schema.agent import AgentResponse, AgentResponseChunk
from jiuwenswarm.gateway.routing.agent_client import AgentServerClient
from jiuwenswarm.telemetry.attributes import (
    APP_ID,
    DOMAIN_ID,
    ERROR_TYPE,
    JIUWENCLAW_APP_ID,
    JIUWENCLAW_CANCELED,
    JIUWENCLAW_CHANNEL_ID,
    JIUWENCLAW_DOMAIN_ID,
    JIUWENCLAW_REQUEST_ID,
    JIUWENCLAW_SESSION_ID,
    JIUWENCLAW_USER_ID,
    USER_ID,
)
from jiuwenswarm.telemetry.context_propagation import inject_trace_context

logger = logging.getLogger(__name__)

_SPAN_NAME = "jiuwenclaw.gateway.agent.request"
_INSTRUMENTATION_SCOPE = "jiuwenswarm.gateway.client"
_WRAPPER_MARKER = "_jiuwenswarm_gateway_telemetry_client"


def _get_runtime() -> Any:
    from jiuwenswarm.telemetry import get_telemetry_runtime

    return get_telemetry_runtime()


@dataclass
class _ClientSpanHandle:
    span: Span
    context_token: object | None = None
    closed: bool = False

    def attach(self) -> bool:
        if self.closed or self.context_token is not None:
            return False
        try:
            span_context = trace.set_span_in_context(self.span)
            self.context_token = otel_context.attach(span_context)
        except Exception as error:
            logger.debug("[GatewayClientTelemetry] context attach failed: %s", error)
            self.context_token = None
            return False
        return True

    def detach(self) -> None:
        token = self.context_token
        self.context_token = None
        if token is None:
            return
        try:
            otel_context.detach(token)
        except Exception as error:
            logger.debug("[GatewayClientTelemetry] context detach failed: %s", error)

    def finish(
        self,
        *,
        error: BaseException | None = None,
        cancelled: bool = False,
    ) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            if error is None:
                self.span.set_status(StatusCode.OK)
            else:
                self.span.set_attribute(ERROR_TYPE, type(error).__name__)
                if cancelled:
                    self.span.set_attribute(JIUWENCLAW_CANCELED, True)
                self.span.set_status(StatusCode.ERROR, str(error)[:256])
                self.span.record_exception(error)
        except Exception as status_error:
            logger.debug(
                "[GatewayClientTelemetry] terminal span update failed: %s",
                status_error,
            )
        finally:
            self.detach()
            try:
                self.span.end()
            except Exception as end_error:
                logger.debug(
                    "[GatewayClientTelemetry] span end failed: %s",
                    end_error,
                )


class GatewayTelemetryAgentClient(AgentServerClient):
    """Transparent ``AgentServerClient`` proxy owning one Gateway CLIENT span."""

    _jiuwenswarm_gateway_telemetry_client = True

    def __init__(
        self,
        client: AgentServerClient,
        *,
        target_uri: str | None = None,
    ) -> None:
        self._client = client
        self._target_uri = target_uri

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    async def connect(self, uri: str) -> None:
        await self._client.connect(uri)

    async def disconnect(self) -> None:
        await self._client.disconnect()

    def set_or_update_server_config(
        self,
        *,
        config: dict[str, Any],
        env: dict[str, str] | None = None,
    ) -> None:
        self._client.set_or_update_server_config(config=config, env=env)

    async def send_request(self, envelope: E2AEnvelope) -> AgentResponse:
        handle = _open_client_span(
            envelope,
            is_stream=False,
            target_uri=self._target_uri,
        )
        if handle is None:
            return await self._client.send_request(envelope)

        handle.attach()
        try:
            return await self._client.send_request(envelope)
        except asyncio.CancelledError as error:
            handle.finish(error=error, cancelled=True)
            raise
        except Exception as error:
            handle.finish(error=error)
            raise
        finally:
            handle.finish()

    async def send_request_stream(
        self,
        envelope: E2AEnvelope,
    ) -> AsyncIterator[AgentResponseChunk]:
        handle = _open_client_span(
            envelope,
            is_stream=True,
            target_uri=self._target_uri,
        )
        inner_stream = self._client.send_request_stream(envelope)
        terminal_error: BaseException | None = None
        cancelled = False
        try:
            while True:
                if handle is not None:
                    handle.attach()
                try:
                    chunk = await anext(inner_stream)
                finally:
                    if handle is not None:
                        handle.detach()
                yield chunk
        except StopAsyncIteration:
            return
        except asyncio.CancelledError as error:
            terminal_error = error
            cancelled = True
            raise
        except Exception as error:
            terminal_error = error
            raise
        finally:
            close_error: BaseException | None = None
            closer = getattr(inner_stream, "aclose", None)
            if callable(closer):
                if handle is not None:
                    handle.attach()
                try:
                    await closer()
                except BaseException as error:
                    close_error = error
                    if terminal_error is None:
                        terminal_error = error
                        cancelled = isinstance(error, asyncio.CancelledError)
                finally:
                    if handle is not None:
                        handle.detach()
            if handle is not None:
                handle.finish(error=terminal_error, cancelled=cancelled)
            if close_error is not None and terminal_error is close_error:
                raise close_error


def wrap_gateway_agent_client(
    client: AgentServerClient,
    *,
    target_uri: str | None = None,
) -> AgentServerClient:
    """Wrap ``client`` once while retaining its business behavior."""
    if getattr(client, _WRAPPER_MARKER, False):
        return client
    return GatewayTelemetryAgentClient(client, target_uri=target_uri)


def _open_client_span(
    envelope: E2AEnvelope,
    *,
    is_stream: bool,
    target_uri: str | None,
) -> _ClientSpanHandle | None:
    try:
        runtime = _get_runtime()
        if not runtime.is_unified_active():
            return None
        provider = runtime.tracer_provider
        if provider is None:
            return None
        tracer = provider.get_tracer(_INSTRUMENTATION_SCOPE)
        span = tracer.start_span(
            _SPAN_NAME,
            kind=SpanKind.CLIENT,
            attributes=_span_attributes(
                envelope,
                is_stream=is_stream,
                target_uri=target_uri,
            ),
        )
    except Exception as error:
        logger.debug("[GatewayClientTelemetry] span lookup/open failed: %s", error)
        return None

    handle = _ClientSpanHandle(span=span)
    if not handle.attach():
        handle.finish()
        return None
    try:
        inject_trace_context(_channel_context(envelope))
    except Exception as error:
        logger.debug("[GatewayClientTelemetry] W3C injection failed: %s", error)
    finally:
        handle.detach()
    return handle


def _channel_context(envelope: E2AEnvelope) -> dict[str, Any]:
    carrier = getattr(envelope, "channel_context", None)
    if isinstance(carrier, dict):
        return carrier
    carrier = {}
    setattr(envelope, "channel_context", carrier)
    return carrier


def _span_attributes(
    envelope: E2AEnvelope,
    *,
    is_stream: bool,
    target_uri: str | None,
) -> dict[str, AttributeValue]:
    attributes: dict[str, AttributeValue] = {
        JIUWENCLAW_REQUEST_ID: str(getattr(envelope, "request_id", None) or ""),
        JIUWENCLAW_CHANNEL_ID: str(getattr(envelope, "channel", None) or ""),
        JIUWENCLAW_SESSION_ID: str(getattr(envelope, "session_id", None) or ""),
        "jiuwenclaw.req.method": str(getattr(envelope, "method", None) or ""),
        "jiuwenclaw.stream": bool(is_stream),
    }
    carrier = getattr(envelope, "channel_context", None)
    if isinstance(carrier, dict):
        for primary, alias, key in (
            (USER_ID, JIUWENCLAW_USER_ID, "user_id"),
            (DOMAIN_ID, JIUWENCLAW_DOMAIN_ID, "domain_id"),
            (APP_ID, JIUWENCLAW_APP_ID, "app_id"),
        ):
            value = _string_value(carrier.get(key))
            if value is not None:
                attributes[primary] = value
                attributes[alias] = value

    attributes.update(_target_attributes(target_uri))
    return attributes


def _target_attributes(target_uri: str | None) -> dict[str, AttributeValue]:
    if not target_uri:
        return {}
    try:
        parsed = urlsplit(target_uri)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return {}
    attributes: dict[str, AttributeValue] = {}
    if hostname:
        attributes["server.address"] = hostname
    if port is not None:
        attributes["server.port"] = port
    if parsed.scheme in {"ws", "wss"}:
        attributes["network.protocol.name"] = "websocket"
    return attributes


def _string_value(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


__all__ = [
    "GatewayTelemetryAgentClient",
    "wrap_gateway_agent_client",
]
