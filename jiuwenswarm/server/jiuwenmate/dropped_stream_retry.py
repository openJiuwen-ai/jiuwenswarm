"""Retry a model call when the provider closes a stream before the body finishes.

The anomaly rail already retries a connection drop that produced no output.
Once any stream chunk has been written, that rail leaves the exception alone
so a finished answer is not sent twice. An incomplete chunked body is not a
finished answer: the call is repeated a limited number of times.
"""

from __future__ import annotations

import logging
from typing import Any

from openjiuwen.harness.rails.model_anomaly_detection_rail import ModelAnomalyDetectionRail

logger = logging.getLogger(__name__)

_MAX_DROPPED_STREAM_RETRIES = 2
_DELAYS = (1.0, 2.0)
_DROPPED_MARKERS = (
    "remoteprotocolerror",
    "incomplete chunked read",
    "peer closed connection",
    "apiconnectionerror",
    "connection error",
    "server disconnected",
)
_BLOCK_MARKERS = (
    "insufficient_quota",
    "out of budget",
    "quota exceeded",
    "billing",
    "invalid api key",
    "context length",
    "maximum context",
    "error code: 400",
    "error code: 401",
    "error code: 402",
    "error code: 403",
    "error code: 404",
)


def _exception_text(exc: BaseException | None) -> str:
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while isinstance(current, BaseException) and id(current) not in seen:
        seen.add(id(current))
        parts.append(f"{type(current).__name__}: {current}")
        cause = current.__cause__
        if not isinstance(cause, BaseException) and not current.__suppress_context__:
            cause = current.__context__
        current = cause if isinstance(cause, BaseException) else None
    return " ".join(parts).lower()


def is_dropped_stream(exc: BaseException | None) -> bool:
    """True when the provider closed the stream instead of rejecting the request."""
    text = _exception_text(exc)
    if not text or any(marker in text for marker in _BLOCK_MARKERS):
        return False
    return any(marker in text for marker in _DROPPED_MARKERS)


def request_dropped_stream_retry(ctx: Any) -> bool:
    """Ask the model-call wrapper to repeat a stream the provider closed early."""
    if getattr(ctx, "_retry_request", None) is not None:
        return False
    attempt = int(getattr(ctx, "retry_attempt", 0) or 0)
    if attempt >= _MAX_DROPPED_STREAM_RETRIES:
        return False
    if not is_dropped_stream(getattr(ctx, "exception", None)):
        return False
    delay = _DELAYS[min(attempt, len(_DELAYS) - 1)]
    ctx.request_retry(delay_seconds=delay)
    logger.warning(
        "[JiuwenMate] retrying model call after the provider closed the stream (%d/%d)",
        attempt + 1,
        _MAX_DROPPED_STREAM_RETRIES,
    )
    return True


def install_dropped_stream_retry() -> None:
    """Keep the anomaly rail, then retry a closed stream that already emitted chunks."""
    current = ModelAnomalyDetectionRail.on_model_exception
    if getattr(current, "_jiuwenmate_dropped_stream_retry", False):
        return
    original = current

    async def on_model_exception(self, ctx):
        await original(self, ctx)
        request_dropped_stream_retry(ctx)

    on_model_exception._jiuwenmate_dropped_stream_retry = True  # type: ignore[attr-defined]
    ModelAnomalyDetectionRail.on_model_exception = on_model_exception  # type: ignore[method-assign]
