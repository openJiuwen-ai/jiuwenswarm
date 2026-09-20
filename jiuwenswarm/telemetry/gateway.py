"""Gateway-owned request span and metric lifecycle."""

from __future__ import annotations

import asyncio
import logging
import time
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from threading import Event, Lock
from typing import Any

from opentelemetry import context as otel_context, trace
from opentelemetry.trace import Span, SpanKind, StatusCode
from opentelemetry.util.types import AttributeValue

from jiuwenswarm.extensions.identity_provider import IdentityInfo, IdentityStore
from jiuwenswarm.telemetry.attributes import (
    APP_ID,
    DOMAIN_ID,
    ERROR_TYPE,
    GEN_AI_CONVERSATION_ID,
    GEN_AI_SPAN_TYPE,
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
from jiuwenswarm.telemetry.metrics import metrics_channel_id, metrics_session_id

logger = logging.getLogger(__name__)
_GATEWAY_REQUEST_STACK: ContextVar[tuple[object, ...]] = ContextVar(
    "jiuwenswarm_gateway_request_stack",
    default=(),
)


def _get_runtime() -> Any:
    from jiuwenswarm.telemetry import get_telemetry_runtime

    return get_telemetry_runtime()


@dataclass
class GatewayRequestHandle:
    """Resources owned by one Gateway business request."""

    span: Span | None
    context_token: object | None
    identity_token: Token[IdentityInfo | None] | None
    metric_session_token: Token[str | None] | None
    metric_channel_token: Token[str | None] | None
    started_at: float
    metric_attributes: dict[str, AttributeValue]
    _stack_marker: object | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )
    _stack_token: Token[tuple[object, ...]] | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )
    telemetry_metrics: Any | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )
    _close_lock: Lock = field(
        default_factory=Lock,
        init=False,
        repr=False,
        compare=False,
    )
    _closed: Event = field(
        default_factory=Event,
        init=False,
        repr=False,
        compare=False,
    )

    @property
    def stack_marker(self) -> object | None:
        return self._stack_marker

    @stack_marker.setter
    def stack_marker(self, value: object | None) -> None:
        self._stack_marker = value

    @property
    def stack_token(self) -> Token[tuple[object, ...]] | None:
        return self._stack_token

    @stack_token.setter
    def stack_token(self, value: Token[tuple[object, ...]] | None) -> None:
        self._stack_token = value

    def claim_close(self) -> bool:
        with self._close_lock:
            if self._closed.is_set():
                return False
            if self._stack_marker is not None or self._stack_token is not None:
                _pop_gateway_request_stack(self, strict=True)
            elif (
                self.identity_token is not None
                or self.metric_session_token is not None
                or self.metric_channel_token is not None
            ):
                raise RuntimeError(
                    "gateway request handle belongs to a different context"
                )
            self._closed.set()
            return True


def open_gateway_request(envelope: object) -> GatewayRequestHandle:
    """Open one ``channel.request`` span and inject its W3C wire context."""
    started_at = time.monotonic()
    handle = GatewayRequestHandle(
        span=None,
        context_token=None,
        identity_token=None,
        metric_session_token=None,
        metric_channel_token=None,
        started_at=started_at,
        metric_attributes={},
    )
    try:
        runtime = _get_runtime()
        if not runtime.is_unified_active():
            return handle
    except Exception as error:
        logger.debug("[TelemetryGateway] runtime lookup failed: %s", error)
        return handle

    carrier = _channel_context(envelope)
    identity = _request_identity(envelope, carrier)
    span_attributes = _request_attributes(envelope, identity)
    metric_attributes = {
        JIUWENCLAW_CHANNEL_ID: str(getattr(envelope, "channel", None) or "")
    }
    handle.metric_attributes = metric_attributes
    handle.telemetry_metrics = getattr(runtime, "telemetry_metrics", None)
    try:
        handle.identity_token = IdentityStore.set_identity(identity)
        handle.metric_session_token = metrics_session_id.set(
            str(getattr(envelope, "session_id", None) or "")
        )
        handle.metric_channel_token = metrics_channel_id.set(
            str(getattr(envelope, "channel", None) or "")
        )
        handle.stack_marker = object()
        handle.stack_token = _GATEWAY_REQUEST_STACK.set(
            (*_GATEWAY_REQUEST_STACK.get(), handle.stack_marker)
        )
    except Exception as error:
        logger.warning("[TelemetryGateway] request context bind failed: %s", error)
        _release_request_context(handle)
        handle.telemetry_metrics = None
        return handle
    except BaseException:
        _release_request_context(handle)
        raise

    try:
        # 行为审计共享同一份请求上下文：session/user/request 进
        # SDK ContextVar + session 注册表，供 HTTP 中间件与打点回退。
        from openjiuwen_runtime.foundation import audit

        routing_md = carrier.get("routing") if isinstance(carrier.get("routing"), dict) else {}
        audit.bind_routing(
            session_id=str(getattr(envelope, "session_id", None) or ""),
            user_id=str(identity.user_id or ""),
            request_id=str(getattr(envelope, "request_id", None) or ""),
            channel_id=str(getattr(envelope, "channel", None) or ""),
            group_id=str(routing_md.get("group_id") or ""),
            bot_id=str(routing_md.get("bot_id") or ""),
        )
    except Exception as error:
        logger.warning(
            "[TelemetryGateway] audit routing bind failed: %s: %s",
            type(error).__name__,
            error,
        )

    provider = getattr(runtime, "tracer_provider", None)
    if provider is not None:
        try:
            tracer = provider.get_tracer("jiuwenswarm.gateway")
            handle.span = tracer.start_span(
                "channel.request",
                kind=SpanKind.SERVER,
                attributes=span_attributes,
            )
            span_context = trace.set_span_in_context(handle.span)
            handle.context_token = otel_context.attach(span_context)
            inject_trace_context(carrier)
        except Exception as error:
            logger.warning("[TelemetryGateway] root span open failed: %s", error)
            _release_span_context(handle)
            _end_span(handle.span)
            handle.span = None

    _record_count(handle, "jiuwenclaw.request.count", metric_attributes)
    return handle


def close_gateway_request(
    handle: GatewayRequestHandle,
    *,
    error: BaseException | None = None,
    cancelled: bool = False,
) -> None:
    """Close one Gateway request, recording duration and any terminal error."""
    if handle is None or not handle.claim_close():
        return

    try:
        duration = max(time.monotonic() - handle.started_at, 0.0)
        terminal_error = error
        if cancelled and terminal_error is None:
            terminal_error = asyncio.CancelledError()

        if terminal_error is not None:
            _mark_span_error(handle.span, terminal_error, cancelled=cancelled)
            _record_count(
                handle,
                "jiuwenclaw.request.error.count",
                handle.metric_attributes,
            )
        elif handle.span is not None:
            try:
                handle.span.set_status(StatusCode.OK)
            except Exception as status_error:
                logger.debug(
                    "[TelemetryGateway] set success status failed: %s", status_error
                )

        _record_duration(handle, duration, handle.metric_attributes)
    finally:
        try:
            _release_request_context(handle)
        finally:
            _release_span_context(handle)
            _end_span(handle.span)


def _channel_context(envelope: object) -> dict[str, Any]:
    carrier = getattr(envelope, "channel_context", None)
    if isinstance(carrier, dict):
        return carrier
    carrier = {}
    try:
        setattr(envelope, "channel_context", carrier)
    except Exception as error:
        logger.debug("[TelemetryGateway] channel context attach failed: %s", error)
    return carrier


def _request_identity(
    envelope: object,
    carrier: dict[str, Any],
) -> IdentityInfo:
    try:
        current = IdentityStore.get_identity()
    except Exception:
        current = None
    identity = IdentityInfo(
        user_id=_first_identity_value(
            _carrier_identity_value(carrier, "user_id"),
            getattr(current, "user_id", None),
            getattr(envelope, "user_id", None),
        ),
        domain_id=_first_identity_value(
            _carrier_identity_value(carrier, "domain_id"),
            getattr(current, "domain_id", None),
        ),
        app_id=_first_identity_value(
            _carrier_identity_value(carrier, "app_id"),
            getattr(current, "app_id", None),
        ),
    )
    for key, value in identity.to_dict().items():
        if key != "extra":
            carrier[key] = value
    return identity


def _carrier_identity_value(carrier: dict[str, Any], key: str) -> object:
    direct = carrier.get(key)
    if direct not in (None, ""):
        return direct
    query = carrier.get("query")
    if not isinstance(query, dict):
        return None
    value = query.get(key)
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


def _first_identity_value(*values: object) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _request_attributes(
    envelope: object,
    identity: IdentityInfo,
) -> dict[str, AttributeValue]:
    session_id = str(getattr(envelope, "session_id", None) or "")
    attributes: dict[str, AttributeValue] = {
        JIUWENCLAW_CHANNEL_ID: str(getattr(envelope, "channel", None) or ""),
        JIUWENCLAW_SESSION_ID: session_id,
        JIUWENCLAW_REQUEST_ID: str(getattr(envelope, "request_id", None) or ""),
        GEN_AI_CONVERSATION_ID: session_id,
        GEN_AI_SPAN_TYPE: "workflow",
        "jiuwenclaw.req.method": str(getattr(envelope, "method", None) or ""),
        "jiuwenclaw.stream": bool(getattr(envelope, "is_stream", False)),
    }
    for primary, alias, value in (
        (USER_ID, JIUWENCLAW_USER_ID, identity.user_id),
        (DOMAIN_ID, JIUWENCLAW_DOMAIN_ID, identity.domain_id),
        (APP_ID, JIUWENCLAW_APP_ID, identity.app_id),
    ):
        if value:
            attributes[primary] = value
            attributes[alias] = value
    return attributes


def _mark_span_error(
    span: Span | None,
    error: BaseException,
    *,
    cancelled: bool,
) -> None:
    if span is None:
        return
    try:
        span.set_attribute(ERROR_TYPE, type(error).__name__)
        if cancelled:
            span.set_attribute(JIUWENCLAW_CANCELED, True)
        span.set_status(StatusCode.ERROR, str(error)[:256])
        span.record_exception(error)
    except Exception as span_error:
        logger.debug("[TelemetryGateway] record terminal error failed: %s", span_error)


def _record_count(
    handle: GatewayRequestHandle,
    name: str,
    attributes: dict[str, AttributeValue],
) -> None:
    if handle.telemetry_metrics is None:
        return
    try:
        handle.telemetry_metrics.add(name, 1, attributes)
    except Exception as metric_error:
        logger.debug("[TelemetryGateway] count metric failed: %s", metric_error)


def _record_duration(
    handle: GatewayRequestHandle,
    duration: float,
    attributes: dict[str, AttributeValue],
) -> None:
    if handle.telemetry_metrics is None:
        return
    try:
        handle.telemetry_metrics.record(
            "jiuwenclaw.request.duration",
            duration,
            attributes,
        )
    except Exception as metric_error:
        logger.debug("[TelemetryGateway] duration metric failed: %s", metric_error)


def _release_span_context(handle: GatewayRequestHandle) -> None:
    if handle.context_token is None:
        return
    try:
        otel_context.detach(handle.context_token)
    except Exception as context_error:
        logger.debug("[TelemetryGateway] context detach failed: %s", context_error)
    finally:
        handle.context_token = None


def _release_request_context(handle: GatewayRequestHandle) -> None:
    try:
        _pop_gateway_request_stack(handle, strict=False)
    finally:
        try:
            if handle.metric_channel_token is not None:
                try:
                    metrics_channel_id.reset(handle.metric_channel_token)
                except Exception as channel_error:
                    logger.debug(
                        "[TelemetryGateway] metric channel reset failed: %s",
                        channel_error,
                    )
        finally:
            handle.metric_channel_token = None
            try:
                if handle.metric_session_token is not None:
                    try:
                        metrics_session_id.reset(handle.metric_session_token)
                    except Exception as session_error:
                        logger.debug(
                            "[TelemetryGateway] metric session reset failed: %s",
                            session_error,
                        )
            finally:
                handle.metric_session_token = None
                if handle.identity_token is not None:
                    try:
                        IdentityStore.clear(handle.identity_token)
                    finally:
                        handle.identity_token = None


def _pop_gateway_request_stack(
    handle: GatewayRequestHandle,
    *,
    strict: bool,
) -> None:
    marker = handle.stack_marker
    stack_token = handle.stack_token
    if marker is None or stack_token is None:
        if strict:
            raise RuntimeError(
                "gateway request handle belongs to a different context"
            )
        handle.stack_marker = None
        handle.stack_token = None
        return

    stack = _GATEWAY_REQUEST_STACK.get()
    marker_index = next(
        (index for index, candidate in enumerate(stack) if candidate is marker),
        None,
    )
    if marker_index is None:
        if strict:
            raise RuntimeError(
                "gateway request handle belongs to a different context"
            )
        return
    if marker_index != len(stack) - 1:
        if strict:
            raise RuntimeError(
                "gateway request handles must be closed in LIFO order"
            )
        return

    try:
        _GATEWAY_REQUEST_STACK.reset(stack_token)
    except (RuntimeError, ValueError) as error:
        if strict:
            raise RuntimeError(
                "gateway request handle belongs to a different context"
            ) from error
        return
    handle.stack_marker = None
    handle.stack_token = None


def _end_span(span: Span | None) -> None:
    if span is None:
        return
    try:
        span.end()
    except Exception as span_error:
        logger.debug("[TelemetryGateway] root span end failed: %s", span_error)


__all__ = [
    "GatewayRequestHandle",
    "close_gateway_request",
    "open_gateway_request",
]
