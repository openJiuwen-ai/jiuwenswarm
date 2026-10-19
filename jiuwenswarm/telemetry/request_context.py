"""Request-local trace propagation and root-span binding registries."""

from __future__ import annotations

import time
import logging
from contextvars import ContextVar, Token
from dataclasses import dataclass
from threading import RLock
from typing import Any, Mapping

from opentelemetry import context as otel_context
from opentelemetry.context import Context
from opentelemetry import trace
from opentelemetry.trace import Span
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from jiuwenswarm.extensions.identity_provider import IdentityInfo, IdentityStore
from jiuwenswarm.telemetry.attributes import (
    APP_ID,
    DOMAIN_ID,
    GEN_AI_CONVERSATION_ID,
    JIUWENCLAW_APP_ID,
    JIUWENCLAW_CHANNEL_ID,
    JIUWENCLAW_DOMAIN_ID,
    JIUWENCLAW_REQUEST_ID,
    JIUWENCLAW_SESSION_ID,
    JIUWENCLAW_USER_ID,
    USER_ID,
)
from jiuwenswarm.telemetry.metrics import metrics_channel_id, metrics_session_id

logger = logging.getLogger(__name__)

_REQUEST_BINDING_STACK: ContextVar[tuple[object, ...]] = ContextVar(
    "jiuwenswarm_request_binding_stack",
    default=(),
)
_W3C_TRACE_CONTEXT = TraceContextTextMapPropagator()


@dataclass(frozen=True)
class TraceBindingHandle:
    session_id: str
    request_id: str
    generation: int
    root_span: Span


@dataclass(frozen=True)
class _TraceBinding:
    handle: TraceBindingHandle
    created_at: float


@dataclass
class IncomingRequestBinding:
    otel_token: Token[Context]
    identity_token: Token[IdentityInfo | None]
    metric_session_token: Token[str | None]
    metric_channel_token: Token[str | None]
    _marker: object
    _stack_token: Token[tuple[object, ...]]

    @property
    def marker(self) -> object:
        """Return the identity marker used to enforce LIFO cleanup."""
        return self._marker

    @property
    def stack_token(self) -> Token[tuple[object, ...]]:
        """Return the ContextVar token paired with this request binding."""
        return self._stack_token


class TraceBindingRegistry:
    """Maintain exact request bindings plus the newest live binding per session."""

    def __init__(self, *, max_bindings: int, ttl_seconds: float) -> None:
        if max_bindings <= 0:
            raise ValueError("max_bindings must be greater than zero")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be greater than zero")
        self._max_bindings = max_bindings
        self._ttl_seconds = ttl_seconds
        self._exact: dict[tuple[str, str], _TraceBinding] = {}
        self._latest_session: dict[str, _TraceBinding] = {}
        self._generation = 0
        self._lock = RLock()

    def bind(
        self,
        session_id: str,
        request_id: str,
        root_span: Span,
    ) -> TraceBindingHandle:
        now = time.monotonic()
        with self._lock:
            self._prune_locked(now)
            self._generation += 1
            handle = TraceBindingHandle(
                session_id=session_id,
                request_id=request_id,
                generation=self._generation,
                root_span=root_span,
            )
            binding = _TraceBinding(handle=handle, created_at=now)
            self._exact[(session_id, request_id)] = binding
            self._latest_session[session_id] = binding
            self._limit_locked()
            return handle

    def resolve(self, session_id: str, request_id: str) -> TraceBindingHandle | None:
        with self._lock:
            self._prune_locked(time.monotonic())
            binding = self._exact.get((session_id, request_id))
            return binding.handle if binding is not None else None

    def resolve_session(self, session_id: str) -> TraceBindingHandle | None:
        with self._lock:
            self._prune_locked(time.monotonic())
            binding = self._latest_session.get(session_id)
            return binding.handle if binding is not None else None

    def remove(self, handle: TraceBindingHandle) -> bool:
        key = (handle.session_id, handle.request_id)
        with self._lock:
            binding = self._exact.get(key)
            if binding is None or not self._same_handle(binding.handle, handle):
                return False
            del self._exact[key]
            latest = self._latest_session.get(handle.session_id)
            if latest is binding:
                self._refresh_latest_locked(handle.session_id)
            return True

    def prune(self, now: float | None = None) -> int:
        prune_at = time.monotonic() if now is None else now
        with self._lock:
            return self._prune_locked(prune_at)

    @staticmethod
    def _same_handle(current: TraceBindingHandle, candidate: TraceBindingHandle) -> bool:
        return (
            current.generation == candidate.generation
            and current.root_span is candidate.root_span
        )

    def _prune_locked(self, now: float) -> int:
        expired = [
            key
            for key, binding in self._exact.items()
            if now - binding.created_at >= self._ttl_seconds
        ]
        sessions: set[str] = set()
        for key in expired:
            binding = self._exact.pop(key)
            latest = self._latest_session.get(binding.handle.session_id)
            if latest is binding:
                sessions.add(binding.handle.session_id)
        for session_id in sessions:
            self._refresh_latest_locked(session_id)
        return len(expired)

    def _limit_locked(self) -> None:
        while len(self._exact) > self._max_bindings:
            oldest_key = min(
                self._exact,
                key=lambda key: self._exact[key].handle.generation,
            )
            binding = self._exact.pop(oldest_key)
            session_id = binding.handle.session_id
            if self._latest_session.get(session_id) is binding:
                self._refresh_latest_locked(session_id)

    def _refresh_latest_locked(self, session_id: str) -> None:
        candidates = (
            binding
            for (candidate_session_id, _), binding in self._exact.items()
            if candidate_session_id == session_id
        )
        latest = max(
            candidates,
            key=lambda binding: binding.handle.generation,
            default=None,
        )
        if latest is None:
            self._latest_session.pop(session_id, None)
        else:
            self._latest_session[session_id] = latest


def bind_incoming_request(request: object) -> IncomingRequestBinding:
    """Attach an extracted remote context and bind request-scoped identity."""

    raw_metadata = getattr(request, "metadata", None)
    metadata: Mapping[str, Any]
    if isinstance(raw_metadata, Mapping):
        metadata = raw_metadata
    else:
        metadata = {}
    identity = IdentityInfo(
        user_id=_identity_value(metadata.get("user_id")),
        domain_id=_identity_value(metadata.get("domain_id")),
        app_id=_identity_value(metadata.get("app_id")),
    )
    extracted = _W3C_TRACE_CONTEXT.extract(metadata)
    otel_token = otel_context.attach(extracted)
    try:
        identity_token = IdentityStore.set_identity(identity)
    except BaseException:
        otel_context.detach(otel_token)
        raise
    try:
        metric_session_token = metrics_session_id.set(
            str(getattr(request, "session_id", None) or "")
        )
    except BaseException:
        try:
            IdentityStore.clear(identity_token)
        finally:
            otel_context.detach(otel_token)
        raise
    try:
        metric_channel_token = metrics_channel_id.set(
            str(
                getattr(request, "channel_id", None)
                or getattr(request, "channel", None)
                or ""
            )
        )
    except BaseException:
        try:
            metrics_session_id.reset(metric_session_token)
        finally:
            try:
                IdentityStore.clear(identity_token)
            finally:
                otel_context.detach(otel_token)
        raise
    _bind_incoming_trace_attributes(request, identity)
    _bind_current_request_id(request)
    marker = object()
    stack_token = _REQUEST_BINDING_STACK.set(
        (*_REQUEST_BINDING_STACK.get(), marker)
    )
    return IncomingRequestBinding(
        otel_token=otel_token,
        identity_token=identity_token,
        metric_session_token=metric_session_token,
        metric_channel_token=metric_channel_token,
        _marker=marker,
        _stack_token=stack_token,
    )


def _bind_current_request_id(request: object) -> None:
    """Expose request_id to the logging layer for precise log↔span join.

    Paired with ``open_agent_run_span``'s ``set_current_request_id`` on the
    AgentServer side; on the Gateway side this is the entry point that first
    sees the request_id. Best-effort: never blocks the business request.
    """
    try:
        from openjiuwen.extensions.observability.span_context import (
            set_current_request_id,
        )

        set_current_request_id(str(getattr(request, "request_id", None) or ""))
    except Exception as e:
        logger.warning(f"set diagnosis request id failed: {e}.")
        return


def _bind_incoming_trace_attributes(
    request: object,
    identity: IdentityInfo,
) -> None:
    """Make remote request attributes available to spans created in child tasks."""
    try:
        from jiuwenswarm.telemetry import get_telemetry_runtime

        runtime = get_telemetry_runtime()
        if not runtime.is_unified_active():
            return
        span_registry = runtime.span_registry
        if span_registry is None:
            return
        span_context = trace.get_current_span().get_span_context()
        if not span_context.is_valid:
            return

        session_id = str(getattr(request, "session_id", None) or "")
        params = getattr(request, "params", None)
        mode = str(params.get("mode") or "") if isinstance(params, Mapping) else ""
        request_method = getattr(request, "method", None)
        if request_method in (None, ""):
            request_method = getattr(request, "req_method", None)
        request_method = getattr(request_method, "value", request_method)
        channel_id = (
            getattr(request, "channel_id", None)
            or getattr(request, "channel", None)
            or ""
        )
        attributes: dict[str, Any] = {
            GEN_AI_CONVERSATION_ID: session_id,
            JIUWENCLAW_CHANNEL_ID: str(channel_id),
            JIUWENCLAW_SESSION_ID: session_id,
            JIUWENCLAW_REQUEST_ID: str(
                getattr(request, "request_id", None) or ""
            ),
            "jiuwenclaw.mode": mode,
            "jiuwenclaw.req.method": str(request_method or ""),
            "jiuwenclaw.stream": bool(
                getattr(request, "is_stream", False)
            ),
        }
        for primary, alias, value in (
            (USER_ID, JIUWENCLAW_USER_ID, identity.user_id),
            (DOMAIN_ID, JIUWENCLAW_DOMAIN_ID, identity.domain_id),
            (APP_ID, JIUWENCLAW_APP_ID, identity.app_id),
        ):
            if value:
                attributes[primary] = value
                attributes[alias] = value
        span_registry.bind_trace_attributes(span_context.trace_id, attributes)
    except Exception:
        # Context propagation must never make a business request fail.
        return


def reset_incoming_request(binding: IncomingRequestBinding) -> None:
    """Restore identity and OTel contexts paired with ``bind_incoming_request``."""

    stack = _REQUEST_BINDING_STACK.get()
    marker_index = next(
        (index for index, marker in enumerate(stack) if marker is binding.marker),
        None,
    )
    if marker_index is None:
        return
    if marker_index != len(stack) - 1:
        raise RuntimeError("incoming request bindings must be reset in LIFO order")
    try:
        _REQUEST_BINDING_STACK.reset(binding.stack_token)
    except (RuntimeError, ValueError):
        return
    try:
        metrics_channel_id.reset(binding.metric_channel_token)
    finally:
        try:
            metrics_session_id.reset(binding.metric_session_token)
        finally:
            try:
                IdentityStore.clear(binding.identity_token)
            finally:
                _clear_current_request_id()
                otel_context.detach(binding.otel_token)


def _clear_current_request_id() -> None:
    """Clear the request_id ContextVar paired with ``_bind_current_request_id``."""
    try:
        from openjiuwen.extensions.observability.span_context import (
            clear_current_request_id,
        )

        clear_current_request_id()
    except Exception as e:
        logger.warning(f"Clear request failed:{e}.")
        return


def _identity_value(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


__all__ = [
    "IncomingRequestBinding",
    "TraceBindingHandle",
    "TraceBindingRegistry",
    "bind_incoming_request",
    "reset_incoming_request",
]
