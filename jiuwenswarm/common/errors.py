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
        super().__init__(
            status if status is not None else StatusCode.ERROR,
            msg="" if message is None else str(message),
            details=details,
            cause=cause,
            **kwargs,
        )

    def __str__(self) -> str:
        # Keep the historical message-only rendering when no StatusCode was
        # attached, so existing `str(e)` call sites keep their output.
        if self.status is StatusCode.ERROR and self.message:
            return self.message
        return super().__str__()


class JiuwenToolError(JiuwenError):
    """A tool failed in a way the agent should observe and reason about."""
    recoverable = True
    fatal = False


class JiuwenStoreError(JiuwenError):
    """Persistence failure (session store, vector store, config store)."""
    recoverable = False
    fatal = False


class JiuwenConfigError(JiuwenError):
    """Invalid or missing configuration; retrying will not help."""
    recoverable = False
    fatal = False


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
