"""Format full-duplex provider errors for logs and client responses."""

import os
import re


def safe_error_detail(detail: str) -> str:
    text = str(detail)
    for name, value in os.environ.items():
        if value and name.upper().endswith(("_API_KEY", "_TOKEN", "_PASSWORD", "_SECRET")):
            text = text.replace(value, "******")
    text = re.sub(r"(?i)(\bBearer\s+)\S+", r"\g<1>******", text)
    return text[:2000]


def duplex_failure(exc: BaseException, *, stage: str, request_id: str) -> str:
    detail = safe_error_detail(str(exc))
    message = type(exc).__name__
    if detail:
        message += f": {detail}"
    return f"{stage}失败：{message} (request_id={request_id})"
