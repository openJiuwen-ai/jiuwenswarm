"""Bounded, credential-redacted diagnostics for duplex failures."""

import os


def safe_error_detail(value: object) -> str:
    """Retain the service detail without returning configured API credentials."""
    message = str(value).strip()
    for name, secret in os.environ.items():
        if name.endswith("API_KEY") and secret:
            message = message.replace(secret, "******")
    return message[:1500]


def duplex_failure(exc: Exception, *, stage: str, request_id: object) -> str:
    """Include the exception type even when a timeout has no message."""
    detail = safe_error_detail(exc) or "服务未提供具体原因"
    return f"{stage}失败：{type(exc).__name__}: {detail} [request_id={request_id}]"
