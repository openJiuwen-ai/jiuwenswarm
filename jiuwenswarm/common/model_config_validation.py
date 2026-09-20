"""Shared validation helpers for model configuration values."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

PLACEHOLDER_API_BASES = frozenset({"https://example.com/compatible-mode/v1"})
EXAMPLE_DOMAINS = frozenset({"example.com", "example.org", "example.net"})
# 首次启动时从 .env.template 复制来的占位凭据（见 resources/.env.template）。
PLACEHOLDER_MODEL_NAMES = frozenset({"your-model-name"})
PLACEHOLDER_API_KEYS = frozenset({"sk-xxxxxxxxx"})


@dataclass(frozen=True)
class ModelProbeResult:
    """Successful model connection probe and its user-visible content."""

    response: Any
    content: str


def _model_probe_output(response: Any) -> tuple[str, bool]:
    """Extract display content and decide whether a probe produced output."""
    if hasattr(response, "content"):
        content = response.content
    elif isinstance(response, dict):
        content = response.get("content", "")
    else:
        content = "" if response is None else str(response)

    if isinstance(response, dict):
        reasoning_content = response.get("reasoning_content")
    else:
        reasoning_content = getattr(response, "reasoning_content", None)

    # Reasoning models may spend the small probe budget entirely on
    # reasoning_content and leave the normal content field empty.
    has_text_output = any(
        isinstance(value, str) and bool(value.strip())
        for value in (content, reasoning_content)
    )

    # Some backends report thinking in a field the client does not map (for
    # example, Ollama's "reasoning"). Generated-token usage still proves that
    # the endpoint, credentials, and model are valid.
    usage = (
        response.get("usage_metadata")
        if isinstance(response, dict)
        else getattr(response, "usage_metadata", None)
    )
    output_tokens = (
        usage.get("output_tokens")
        if isinstance(usage, dict)
        else getattr(usage, "output_tokens", None)
    )
    has_output = has_text_output or (
        isinstance(output_tokens, (int, float)) and output_tokens > 0
    )
    return content if isinstance(content, str) else "", has_output


async def probe_model_connection(
    model: Any,
    *,
    token_limits: Sequence[int] = (3, 16),
    invoke_kwargs: Mapping[str, Any] | None = None,
    timeout_seconds: float | None = None,
    log_context: str = "model connection probe",
) -> ModelProbeResult:
    """Probe a model using each output-token allowance until one succeeds.

    Both an invocation exception and a response without content, reasoning, or
    generated-token usage count as a failed attempt. The final failure is
    raised to the caller so each subsystem can format its own error response.
    ``timeout_seconds`` bounds each complete invocation even when a model
    client ignores its own timeout setting.
    """
    extra_kwargs = dict(invoke_kwargs or {})
    limits = tuple(token_limits)
    if not limits:
        raise ValueError("token_limits must contain at least one value")

    for attempt, max_tokens in enumerate(limits):
        try:
            invocation = model.invoke(
                messages=[{"role": "user", "content": "Hi"}],
                max_tokens=max_tokens,
                **extra_kwargs,
            )
            response = (
                await invocation
                if timeout_seconds is None
                else await asyncio.wait_for(invocation, timeout=timeout_seconds)
            )
            content, has_output = _model_probe_output(response)
            if not has_output:
                raise ValueError("Empty response from model")
            return ModelProbeResult(response=response, content=content)
        except Exception as exc:  # noqa: BLE001
            if attempt + 1 >= len(limits):
                raise
            next_max_tokens = limits[attempt + 1]
            logger.info(
                "[%s] max_tokens=%d failed, retrying with %d: %s",
                log_context,
                max_tokens,
                next_max_tokens,
                exc,
            )

    raise AssertionError("model probe attempts unexpectedly exhausted")


def is_placeholder_api_base(api_base: str) -> bool:
    """Return True when api_base is a documentation placeholder URL."""
    value = str(api_base or "").strip()
    if not value:
        return False
    if value in PLACEHOLDER_API_BASES:
        return True
    try:
        host = urlparse(value).hostname or ""
    except Exception:
        return False
    if not host:
        return False
    return any(host == domain or host.endswith(f".{domain}") for domain in EXAMPLE_DOMAINS)


def is_placeholder_model_entry(mcc: dict | None) -> bool:
    """Return True when mcc still carries first-run placeholder credentials.

    只识别明确的占位值（模板 URL/占位 model_name/占位 api_key），不会误伤
    "api_base 为真实地址、空 key" 的本地 vLLM 等合法无鉴权部署。
    """
    mcc = mcc or {}
    if is_placeholder_api_base(str(mcc.get("api_base", "") or "").strip()):
        return True
    if str(mcc.get("model_name", "") or "").strip() in PLACEHOLDER_MODEL_NAMES:
        return True
    if str(mcc.get("api_key", "") or "").strip() in PLACEHOLDER_API_KEYS:
        return True
    return False


def model_client_config_view(mcc: Any) -> dict[str, Any]:
    """Normalize openjiuwen ``ModelClientConfig`` (or a plain dict) for the checks above.

    ``ModelClientConfig`` is a pydantic/dataclass-like object built by
    ``build_model_from_entry``; reduce it to the 3 fields these helpers read.
    """
    if isinstance(mcc, dict):
        return {
            "api_base": mcc.get("api_base", "") or "",
            "api_key": mcc.get("api_key", "") or "",
            "model_name": mcc.get("model_name", "") or "",
        }
    return {
        "api_base": getattr(mcc, "api_base", None) or "",
        "api_key": getattr(mcc, "api_key", None) or "",
        "model_name": getattr(mcc, "model_name", None) or "",
    }
