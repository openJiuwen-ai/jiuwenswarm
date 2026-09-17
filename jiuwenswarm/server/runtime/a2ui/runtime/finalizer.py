# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Final validation and repair for model-emitted A2UI responses."""

from __future__ import annotations

import inspect
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from jiuwenswarm.server.runtime.a2ui.protocol import A2UI_OPEN_TAG, get_protocol_spec


RepairCall = Callable[[str], Any]
logger = logging.getLogger(__name__)

_A2UI_PROTOCOL_LINE_RE = re.compile(
    r'(?im)^\s*(?:[\[{,]\s*)*"?(?:beginRendering|surfaceUpdate|dataModelUpdate|deleteSurface)"?\s*(?::|$)'
)


@dataclass(frozen=True)
class A2UIFinalizationResult:
    """Structured finalization result for retry decisions."""

    content: str
    status: str
    validation_error: str | None = None


def _coerce_model_message_content(message: Any) -> str:
    if isinstance(message, str):
        return message
    if isinstance(message, dict):
        value = message.get("content") or message.get("output") or ""
        return value if isinstance(value, str) else str(value)
    value = getattr(message, "content", None)
    if isinstance(value, str):
        return value
    value = getattr(message, "output", None)
    if isinstance(value, str):
        return value
    return str(message) if message is not None else ""


def _a2ui_failure_text(content: str, validation_error: str) -> str:
    spec = get_protocol_spec()
    readable = spec.format_for_text_channel(content)
    if readable:
        return readable
    _ = validation_error
    return "界面内容生成失败，请重试或换一种方式描述你的需求。"


def has_a2ui_protocol_marker(content: str) -> bool:
    """Return True when content looks like an A2UI payload or fragment."""
    text = content or ""
    return A2UI_OPEN_TAG in text or bool(_A2UI_PROTOCOL_LINE_RE.search(text))


def should_finalize_a2ui_content(content: str) -> bool:
    """Return True when the response should enter A2UI validation/repair."""
    return isinstance(content, str) and has_a2ui_protocol_marker(content)


class A2UIResponseFinalizer:
    """Validate, repair, or safely degrade a complete assistant response."""

    async def finalize(
            self,
            content: str,
            *,
            user_query: Any,
            request_id: str,
            repair_call: RepairCall | None,
            max_repair_attempts: int = 2,
    ) -> str:
        result = await self.finalize_result(
            content,
            user_query=user_query,
            request_id=request_id,
            repair_call=repair_call,
            max_repair_attempts=max_repair_attempts,
        )
        return result.content

    async def finalize_result(
            self,
            content: str,
            *,
            user_query: Any,
            request_id: str,
            repair_call: RepairCall | None,
            max_repair_attempts: int = 2,
    ) -> A2UIFinalizationResult:
        _ = request_id
        if not should_finalize_a2ui_content(content):
            return A2UIFinalizationResult(content=content, status="skipped")

        spec = get_protocol_spec()
        started_at = time.perf_counter()
        logger.info(
            "A2UI finalizer validating: request_id=%s content_chars=%d",
            request_id,
            len(content or ""),
        )
        validation = spec.validate_response(content)
        logger.info(
            "A2UI finalizer validation complete: request_id=%s valid=%s duration_ms=%.1f",
            request_id,
            validation.valid,
            (time.perf_counter() - started_at) * 1000,
        )
        if validation.valid:
            if A2UI_OPEN_TAG in content or spec.may_contain_a2ui_content(content):
                return A2UIFinalizationResult(content=content, status="valid")
            last_error = (
                "A2UI-like content was emitted without a valid <a2ui-json> block "
                "or raw A2UI JSON message list."
            )
        else:
            last_error = validation.error

        repaired_content = content
        for _attempt in range(1, max_repair_attempts + 1):
            if repair_call is None:
                break
            logger.info(
                "A2UI finalizer repair attempt started: request_id=%s attempt=%d",
                request_id,
                _attempt,
            )
            prompt = spec.build_repair_prompt(
                invalid_content=repaired_content,
                validation_error=last_error,
                user_query=str(user_query or ""),
            )
            response = repair_call(prompt)
            if inspect.isawaitable(response):
                response = await response
            repaired_content = _coerce_model_message_content(response)
            validation = spec.validate_response(repaired_content)
            logger.info(
                "A2UI finalizer repair attempt complete: request_id=%s attempt=%d valid=%s",
                request_id,
                _attempt,
                validation.valid,
            )
            if validation.valid:
                return A2UIFinalizationResult(content=repaired_content, status="repaired")
            last_error = validation.error

        return A2UIFinalizationResult(
            content=_a2ui_failure_text(repaired_content, last_error),
            status="repair_failed",
            validation_error=last_error,
        )


__all__ = [
    "A2UIFinalizationResult",
    "A2UIResponseFinalizer",
    "has_a2ui_protocol_marker",
    "should_finalize_a2ui_content",
]
