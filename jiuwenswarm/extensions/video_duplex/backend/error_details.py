"""Bounded, credential-redacted diagnostics for duplex failures."""

import os
import re


def safe_error_detail(value: object) -> str:
    """Retain the service detail without returning configured API credentials."""
    message = str(value).strip()
    for name, secret in os.environ.items():
        if name.upper().endswith(("API_KEY", "TOKEN", "PASSWORD", "SECRET", "PRIVATE_KEY")) and secret:
            message = message.replace(secret, "******")
            if len(secret) >= 16:
                # Providers may echo only the beginning/end of a rejected key.
                # Require eight known characters; short fragments match ordinary text.
                token_chars = r"[A-Za-z0-9_+/=.*\u2026-]*"
                for fragment in (secret[:8], secret[-8:]):
                    message = re.sub(
                        token_chars + re.escape(fragment) + token_chars, "******", message,
                    )
    return message[:1500]


def duplex_failure(exc: Exception, *, stage: str, request_id: object) -> str:
    """Include the exception type even when a timeout has no message."""
    detail = safe_error_detail(exc) or "服务未提供具体原因"
    return f"{stage}失败：{type(exc).__name__}: {detail} [request_id={request_id}]"
