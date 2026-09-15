# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Request-scoped accounting for auxiliary LLM calls."""

from __future__ import annotations

import logging
import time
import uuid
from contextvars import ContextVar, Token
from typing import Any, Awaitable, Callable

import httpx

logger = logging.getLogger(__name__)

AuxLlmUsageSink = Callable[[Any], Awaitable[None]]
_AUX_LLM_USAGE_SINK: ContextVar[AuxLlmUsageSink | None] = ContextVar(
    "jiuwenclaw_aux_llm_usage_sink",
    default=None,
)


def bind_aux_llm_usage_sink(sink: AuxLlmUsageSink) -> Token:
    """Bind the adapter-owned sink for the current request context."""
    return _AUX_LLM_USAGE_SINK.set(sink)


def reset_aux_llm_usage_sink(token: Token) -> None:
    _AUX_LLM_USAGE_SINK.reset(token)


def _serialize_llm_usage(usage_metadata: Any) -> dict[str, Any]:
    if isinstance(usage_metadata, dict):
        return dict(usage_metadata)
    for method_name in ("model_dump", "dict"):
        serializer = getattr(usage_metadata, method_name, None)
        if not callable(serializer):
            continue
        try:
            payload = serializer()
        except Exception:  # noqa: BLE001 -- try the next representation
            continue
        if isinstance(payload, dict):
            return payload
    result: dict[str, Any] = {}
    for key in (
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cache_tokens",
        "cache_read_input_tokens",
        "input_cost",
        "output_cost",
        "total_cost",
    ):
        value = getattr(usage_metadata, key, None)
        if value is not None:
            result[key] = value
    return result


def build_late_llm_usage_reporter(
    request_params: Any,
    session_id: str,
) -> AuxLlmUsageSink | None:
    """Build an authenticated callback sink for usage arriving after chat.done."""
    if not isinstance(request_params, dict):
        return None
    office_claw_mcp = request_params.get("office_claw_mcp")
    if not isinstance(office_claw_mcp, dict):
        return None
    env = office_claw_mcp.get("env")
    if not isinstance(env, dict):
        return None
    api_url = str(env.get("OFFICE_CLAW_API_URL") or "").strip().rstrip("/")
    invocation_id = str(env.get("OFFICE_CLAW_INVOCATION_ID") or "").strip()
    callback_token = str(env.get("OFFICE_CLAW_CALLBACK_TOKEN") or "").strip()
    if not all((api_url, invocation_id, callback_token, session_id)):
        return None

    async def _report(usage_metadata: Any) -> None:
        serialized = _serialize_llm_usage(usage_metadata)
        usage_candidates = {
            "inputTokens": serialized.get("input_tokens"),
            "outputTokens": serialized.get("output_tokens"),
            "cacheReadTokens": serialized.get(
                "cache_tokens",
                serialized.get("cache_read_input_tokens"),
            ),
            "costUsd": serialized.get("total_cost"),
        }
        usage: dict[str, int | float] = {}
        for key, value in usage_candidates.items():
            if isinstance(value, (int, float)) and value >= 0:
                usage[key] = value
        if not usage:
            return
        usage_event_id = uuid.uuid4().hex
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                f"{api_url}/api/callbacks/report-llm-usage",
                json={
                    "invocationId": invocation_id,
                    "callbackToken": callback_token,
                    "sessionId": session_id,
                    "usageEventId": usage_event_id,
                    "usage": usage,
                    "timestamp": int(time.time() * 1000),
                },
            )
            response.raise_for_status()
        logger.info(
            "[llm_usage] late auxiliary usage reported: session_id=%s usage_event_id=%s",
            session_id,
            usage_event_id,
        )

    return _report


async def emit_llm_usage_to_session(session: Any, usage_metadata: Any) -> None:
    """Report auxiliary usage to the active request, falling back to the session stream."""
    if not usage_metadata:
        return

    sink = _AUX_LLM_USAGE_SINK.get()
    if sink is not None:
        try:
            await sink(usage_metadata)
            return
        except Exception:  # noqa: BLE001 -- retain the legacy stream fallback
            logger.warning("[llm_usage] request sink failed", exc_info=True)

    if session is None:
        return
    payload = _serialize_llm_usage(usage_metadata)
    if not payload:
        return
    try:
        from openjiuwen.core.session.stream import OutputSchema  # type: ignore

        await session.write_stream(
            OutputSchema(
                type="llm_usage",
                index=0,
                payload={"usage_metadata": payload},
            )
        )
    except Exception:  # noqa: BLE001 -- accounting must not break the agent result
        logger.warning("[llm_usage] auxiliary usage.stream_failed", exc_info=True)


class AuxiliaryUsageReportingModel:
    """Delegate model calls while reporting direct ``invoke`` usage.

    Evolution components call their model directly instead of going through the
    agent telemetry rail.  Keeping this wrapper local to the evolution rail
    avoids double-counting normal agent calls while forwarding their response
    usage to the request-scoped accounting sink.
    """

    def __init__(
        self,
        delegate: Any,
        usage_sink: AuxLlmUsageSink | None = None,
    ) -> None:
        self._delegate = delegate
        self._usage_sink = usage_sink

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    async def _report_usage(self, usage_metadata: Any) -> None:
        if self._usage_sink is not None:
            await self._usage_sink(usage_metadata)
            return
        await emit_llm_usage_to_session(None, usage_metadata)

    async def invoke(self, *args: Any, **kwargs: Any) -> Any:
        response = await self._delegate.invoke(*args, **kwargs)
        await self._report_usage(getattr(response, "usage_metadata", None))
        return response

    async def stream(self, *args: Any, **kwargs: Any):
        """Delegate streaming calls and forward the final usage metadata once."""
        usage_metadata = None
        async for chunk in self._delegate.stream(*args, **kwargs):
            chunk_usage = getattr(chunk, "usage_metadata", None)
            if chunk_usage is not None:
                usage_metadata = chunk_usage
            yield chunk
        if usage_metadata is not None:
            await self._report_usage(usage_metadata)


__all__ = [
    "AuxiliaryUsageReportingModel",
    "AuxLlmUsageSink",
    "bind_aux_llm_usage_sink",
    "build_late_llm_usage_reporter",
    "emit_llm_usage_to_session",
    "reset_aux_llm_usage_sink",
]
