# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""JiuwenSwarm 统一异常基类.

Bridges jiuwenswarm's app-level exception hierarchies (TeamError, A2XError,
LLMClientError, ...) onto the openjiuwen framework taxonomy (BaseError), so
every error carries a stable ``code``, ``recoverable``/``fatal`` semantics and
``to_dict()`` for logs / API responses, and boundary handlers can catch one
root type for both SDK and app errors.

Call sites keep the message-first style (``TeamError("boom")``); a StatusCode
can be attached when known (``TeamError("boom", status=StatusCode....)``).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.exception.errors import BaseError

_logger = logging.getLogger(__name__)


class JiuwenError(BaseError):
    """Message-first adapter over openjiuwen's BaseError.

    Also accepts BaseError's status-first signature so ``BaseError.__reduce__``
    (pickling) and StatusCode-style raises keep working.
    """

    def __init__(
        self,
        message: Any = "",
        *,
        status: Optional[StatusCode] = None,
        msg: Optional[str] = None,
        details: Optional[Any] = None,
        cause: Optional[BaseException] = None,
        **kwargs: Any,
    ):
        if isinstance(message, StatusCode):
            # BaseError-style call: JiuwenError(StatusCode.X, msg=...)
            status = message if status is None else status
            message = msg
        elif not message and msg is not None:
            message = msg
        # Message-first calls (TeamError("boom"), A2XError("boom"), ...) never
        # attach a StatusCode, so without this flag _render_message() would
        # fall back to StatusCode.ERROR's generic "error" template while
        # str(e)/message show the real text - making args, to_dict()
        # and str() disagree with each other. See _render_message below.
        self._status_given = status is not None
        self._raw_message = "" if message is None else str(message)
        super().__init__(
            status if status is not None else StatusCode.ERROR,
            msg=self._raw_message,
            details=details,
            cause=cause,
            **kwargs,
        )

    def _render_message(self) -> str:
        if not self._status_given and self._raw_message:
            return self._raw_message
        return super()._render_message()

    def __str__(self) -> str:
        # Keep the historical message-only rendering when no StatusCode was
        # attached, so existing `str(e)` call sites keep their output.
        # Use getattr defensively: __str__ must never raise, even if called
        # on a not-fully-initialized instance (e.g. during unpickling).
        status = getattr(self, "status", None)
        message = getattr(self, "message", None)
        if status is StatusCode.ERROR and message:
            return message
        return super().__str__()

    def __reduce__(self):
        # BaseError.__reduce__ always replays through the StatusCode-first
        # path (`cls(status, msg=...)`), which would mark a reconstructed
        # message-first error as "status given" and reintroduce the
        # args/to_dict contradiction _render_message fixes above. Replay
        # with None instead when no status was ever attached, so unpickling
        # reproduces the original message-first instance.
        status = self.status if self._status_given else None
        return (
            self.__class__._reconstruct,
            (status, self.message, self.details, self.cause, self.params),
            None,
        )

    @classmethod
    def _reconstruct(cls, status, msg, details, cause, params):
        # Override BaseError._reconstruct to pass `status` as an explicit
        # keyword rather than relying on the message/StatusCode sniffing in
        # __init__ matching BaseError.__reduce__'s positional argument order.
        params = params or {}
        return cls(status=status, msg=msg, details=details, cause=cause, **params)

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        # Every message-first raise collapses onto StatusCode.ERROR(-1), so
        # `code`/`status` alone can't tell a TeamCreateError from an
        # A2XConnectionError. The class itself is the one identifier that's
        # already stable and unique per error kind, so surface it until
        # module-based error codes exist.
        data["error_type"] = type(self).__qualname__
        return data


class JiuwenToolError(JiuwenError):
    """A tool failed in a way the agent should observe and reason about."""
    recoverable = True
    fatal = False


class JiuwenStoreError(JiuwenError):
    """Persistence failure (session store, vector store, config store).

    recoverable=True matches openjiuwen SDK's StoreError (an ExecutionError):
    a store call is usually worth retrying. It was previously False, the
    opposite of the SDK's verdict, so a boundary handler that trusted
    `.recoverable` would retry an SDK StoreError but give up on the
    equivalent jiuwenswarm one.
    """
    recoverable = True
    fatal = False


class JiuwenConfigError(JiuwenError):
    """Invalid or missing configuration; retrying will not help.

    fatal=True matches openjiuwen SDK's ConfigurationError (a
    FrameworkError, "must abort current execution"). It was previously
    False, the opposite of the SDK's verdict, so a boundary handler that
    trusted `.fatal` would abort on an SDK ConfigurationError but keep going
    on the equivalent jiuwenswarm one.
    """
    recoverable = False
    fatal = True


def record_boundary_exception(boundary: str, exc: BaseException) -> None:
    """Record an exception on the active OTel span at a designated boundary.

    Piggybacks on the ``team_observability`` TracerProvider (no-op tracer when
    disabled). If no span is active, opens a short error span so the failure
    still reaches the trace file / OTLP collector. Never raises: telemetry
    must not mask or replace the original error.
    """
    try:
        from opentelemetry import trace
        from opentelemetry.trace import Status, StatusCode as OtelStatusCode

        code = getattr(exc, "code", None) or type(exc).__name__
        span = trace.get_current_span()
        if span is not None and span.is_recording():
            span.record_exception(exc)
            span.set_status(Status(OtelStatusCode.ERROR, str(code)))
            return
        tracer = trace.get_tracer("jiuwenswarm.boundary")
        with tracer.start_as_current_span(f"boundary.{boundary}.error") as err_span:
            err_span.record_exception(exc)
            err_span.set_status(Status(OtelStatusCode.ERROR, str(code)))
    except Exception:  # noqa: BLE001 - telemetry failure must never propagate
        _logger.debug("record_boundary_exception failed for %s", boundary, exc_info=True)
