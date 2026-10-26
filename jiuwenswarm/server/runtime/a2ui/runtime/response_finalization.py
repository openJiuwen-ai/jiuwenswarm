# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Runtime helpers for validating and repairing assistant A2UI responses."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable

from jiuwenswarm.server.runtime.a2ui.parser import (
    coerce_message_list,
    iter_tagged_block_bodies,
    strip_tagged_a2ui_blocks,
)
from jiuwenswarm.server.runtime.a2ui.protocol import get_protocol_spec
from jiuwenswarm.server.runtime.a2ui.runtime.finalizer import (
    A2UIResponseFinalizer,
    RepairCall,
    should_finalize_a2ui_content,
)


logger = logging.getLogger(__name__)

# Type for a function that can retry a request without A2UI
RetryWithoutA2UI = Callable[[str], Any]
_A2UI_FINALIZATION_TIMEOUT_SECONDS = 45.0
_A2UI_FAST_PATH_VALIDATION_TIMEOUT_SECONDS = 5.0


async def finalize_a2ui_assistant_content(
    content: str,
    *,
    user_query: Any,
    request_id: str,
    repair_call: RepairCall | None,
    a2ui_enabled: bool,
    retry_without_a2ui_call: RetryWithoutA2UI | None = None,
) -> str:
    """Validate and repair a complete assistant response when A2UI is enabled.
    
    Flow:
    1. Validate A2UI content
    2. If invalid, try to repair (up to 2 times)
    3. If repair fails, retry user request WITHOUT A2UI prompt
    4. Return repaired A2UI, plain retry result, or safe plain text
    """
    if not a2ui_enabled or not isinstance(content, str) or not should_finalize_a2ui_content(content):
        return content

    started_at = time.perf_counter()
    logger.info(
        "A2UI response finalization started: request_id=%s has_tag=%s",
        request_id,
        "<a2ui-json>" in content,
    )

    fast_path_valid, fast_path_error = await _validate_parseable_tagged_a2ui_fast_path(content)
    if fast_path_valid:
        logger.info(
            "A2UI response finalization fast-path schema-valid: request_id=%s duration_ms=%.1f",
            request_id,
            (time.perf_counter() - started_at) * 1000,
        )
        return content
    if fast_path_error == "timeout":
        logger.warning(
            "A2UI response finalization fast-path validation timed out: request_id=%s timeout_s=%.1f duration_ms=%.1f",
            request_id,
            _A2UI_FAST_PATH_VALIDATION_TIMEOUT_SECONDS,
            (time.perf_counter() - started_at) * 1000,
        )
        return _a2ui_timeout_fallback(content)
    if fast_path_error:
        logger.info(
            "A2UI response finalization fast-path rejected: request_id=%s error=%s",
            request_id,
            fast_path_error,
        )

    try:
        finalization = await asyncio.wait_for(
            A2UIResponseFinalizer().finalize_result(
                content,
                user_query=user_query,
                request_id=request_id,
                repair_call=repair_call,
            ),
            timeout=_A2UI_FINALIZATION_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "A2UI response finalization timed out: request_id=%s timeout_s=%.1f duration_ms=%.1f",
            request_id,
            _A2UI_FINALIZATION_TIMEOUT_SECONDS,
            (time.perf_counter() - started_at) * 1000,
        )
        return _a2ui_timeout_fallback(content)
    except Exception as exc:  # noqa: BLE001
        logger.exception("A2UI response finalization failed: request_id=%s error=%s", request_id, exc)
        return content

    finalized = finalization.content
    logger.info(
        "A2UI response finalization status: request_id=%s status=%s duration_ms=%.1f",
        request_id,
        finalization.status,
        (time.perf_counter() - started_at) * 1000,
    )
    if finalization.status == "repair_failed":
        logger.info(
            "A2UI repair failed, retrying without A2UI: request_id=%s",
            request_id,
        )
        # Retry without A2UI prompt
        if retry_without_a2ui_call is not None:
            try:
                retry_response = retry_without_a2ui_call(str(user_query or ""))
                import inspect
                if inspect.isawaitable(retry_response):
                    retry_response = await retry_response
                retry_content = _coerce_model_message_content(retry_response)
                if retry_content:
                    logger.info(
                        "Retry without A2UI succeeded: request_id=%s",
                        request_id,
                    )
                    return retry_content
            except Exception as retry_exc:
                logger.exception(
                    "Retry without A2UI failed: request_id=%s error=%s",
                    request_id,
                    retry_exc,
                )
    
    if finalized != content:
        logger.info("A2UI response finalized: request_id=%s changed=true", request_id)
    return finalized


def _has_parseable_tagged_a2ui_blocks(content: str) -> bool:
    blocks = iter_tagged_block_bodies(content or "")
    if not blocks:
        return False
    for _, body in blocks:
        try:
            parsed = json.loads(body.strip())
        except json.JSONDecodeError:
            return False
        if coerce_message_list(parsed) is None:
            return False
    return True


async def _validate_parseable_tagged_a2ui_fast_path(content: str) -> tuple[bool, str | None]:
    if not _has_parseable_tagged_a2ui_blocks(content):
        return False, None

    spec = get_protocol_spec()
    try:
        validation = await asyncio.wait_for(
            asyncio.to_thread(spec.validate_response, content),
            timeout=_A2UI_FAST_PATH_VALIDATION_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        return False, "timeout"
    except Exception as exc:  # noqa: BLE001
        logger.exception("A2UI fast-path schema validation failed unexpectedly: error=%s", exc)
        return False, str(exc)

    return validation.valid, validation.error or None


def _a2ui_timeout_fallback(content: str) -> str:
    try:
        readable = get_protocol_spec().format_for_text_channel(content)
        if readable:
            return readable
    except Exception as exc:  # noqa: BLE001
        logger.exception("A2UI timeout fallback formatting failed: error=%s", exc)
    stripped = strip_tagged_a2ui_blocks(content or "")
    if stripped:
        return stripped
    return "A2UI 界面生成超时，请重试或改用普通文本结果。"


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


__all__ = ["finalize_a2ui_assistant_content"]
