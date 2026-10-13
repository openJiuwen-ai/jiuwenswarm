# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Server-enforced limits for the sandbox control plane."""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_MAX_FILE_MB = 100
DEFAULT_EXEC_TIMEOUT_SECONDS = 600
MAX_FILE_MB = 1024
MAX_EXEC_TIMEOUT_SECONDS = 3600
UPLOAD_CHUNK_BYTES = 64 * 1024
MAX_JSON_BODY_BYTES = 2 * 1024 * 1024
MULTIPART_OVERHEAD_BYTES = 1024 * 1024
MAX_STDIN_CHARS = 1024 * 1024
MAX_COMMAND_ITEMS = 256
MAX_ENV_ITEMS = 1024
MAX_SEARCH_PATTERN_CHARS = 4096
MAX_EXCLUDE_PATTERNS = 256
MAX_LIST_RESULTS = 10_000
MAX_TEXT_RESPONSE_BYTES = 4 * 1024 * 1024


class SandboxLimitError(ValueError):
    def __init__(self, code: str, message: str, *, status_code: int):
        self.code = code
        self.status_code = status_code
        super().__init__(message)


class _LimitedReceive:
    def __init__(self, receive, maximum: int, code: str) -> None:
        self._receive = receive
        self._maximum = maximum
        self._code = code
        self._received = 0
        # FastAPI turns any exception raised while parsing a body into a 400,
        # so the middleware reads this instead of relying on the exception.
        self.error: SandboxLimitError | None = None

    async def __call__(self):
        message = await self._receive()
        if message.get("type") == "http.request":
            self._received += len(message.get("body") or b"")
            if self._received > self._maximum:
                self.error = SandboxLimitError(
                    self._code,
                    f"request body exceeds limit of {self._maximum} bytes",
                    status_code=413,
                )
                raise self.error
        return message


def limit_asgi_receive(
    receive, maximum: int, *, code: str = "SANDBOX_UPLOAD_TOO_LARGE"
) -> _LimitedReceive:
    """Stop reading an ASGI body once ``maximum`` bytes have arrived.

    Used for chunked requests that do not send Content-Length. The returned
    receive callable raises ``SandboxLimitError`` instead of buffering the
    rest of the body, and keeps it on ``.error``.
    """
    return _LimitedReceive(receive, maximum, code)


def _positive_env(name: str, default: int, maximum: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not 1 <= value <= maximum:
        raise ValueError(f"{name} must be in [1, {maximum}]")
    return value


@dataclass(frozen=True)
class SandboxLimits:
    max_file_mb: int = DEFAULT_MAX_FILE_MB
    exec_timeout_seconds: int = DEFAULT_EXEC_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls) -> SandboxLimits:
        return cls(
            max_file_mb=_positive_env(
                "JIUWENBOX_MAX_FILE_MB", DEFAULT_MAX_FILE_MB, MAX_FILE_MB
            ),
            exec_timeout_seconds=_positive_env(
                "JIUWENBOX_EXEC_TIMEOUT_SECONDS",
                DEFAULT_EXEC_TIMEOUT_SECONDS,
                MAX_EXEC_TIMEOUT_SECONDS,
            ),
        )

    @property
    def max_file_bytes(self) -> int:
        return self.max_file_mb * 1024 * 1024

    def enforce_file_size(self, size: int, *, direction: str) -> None:
        if size <= self.max_file_bytes:
            return
        raise self.file_too_large(
            direction,
            f"{direction} file is {size} bytes; limit is {self.max_file_bytes} bytes",
        )

    def file_too_large(self, direction: str, message: str | None = None) -> SandboxLimitError:
        code = (
            "SANDBOX_UPLOAD_TOO_LARGE"
            if direction == "upload"
            else "SANDBOX_DOWNLOAD_TOO_LARGE"
        )
        return SandboxLimitError(
            code,
            message or f"{direction} file exceeds limit of {self.max_file_bytes} bytes",
            status_code=413,
        )

    def effective_exec_timeout(self, requested: float | None) -> float:
        if requested is None:
            return float(self.exec_timeout_seconds)
        if requested <= 0:
            raise SandboxLimitError(
                "SANDBOX_INVALID_TIMEOUT",
                "timeout_seconds must be greater than zero",
                status_code=400,
            )
        return min(float(requested), float(self.exec_timeout_seconds))

    def enforce_result_count(self, count: int) -> None:
        if count > MAX_LIST_RESULTS:
            raise SandboxLimitError(
                "SANDBOX_RESULT_LIMIT_EXCEEDED",
                f"result contains {count} items; limit is {MAX_LIST_RESULTS}",
                status_code=413,
            )

    def enforce_text_response(self, value: str) -> None:
        size = len(value.encode("utf-8"))
        if size > MAX_TEXT_RESPONSE_BYTES:
            raise SandboxLimitError(
                "SANDBOX_RESPONSE_TOO_LARGE",
                f"text response is {size} bytes; limit is {MAX_TEXT_RESPONSE_BYTES} bytes",
                status_code=413,
            )

    @property
    def download_fallback_output_bytes(self) -> int:
        """Exec output cap for the base64 download fallback.

        Base64 of ``max_file_bytes`` plus the normal text budget for stderr,
        so the fallback accepts every file the IPC path accepts.
        """
        return 4 * ((self.max_file_bytes + 2) // 3) + MAX_TEXT_RESPONSE_BYTES

    def enforce_exec_output(
        self,
        stdout: str | None,
        stderr: str | None,
        *,
        limit: int = MAX_TEXT_RESPONSE_BYTES,
    ) -> None:
        size = len((stdout or "").encode("utf-8")) + len((stderr or "").encode("utf-8"))
        if size > limit:
            raise SandboxLimitError(
                "SANDBOX_EXEC_OUTPUT_TOO_LARGE",
                f"exec output is {size} bytes; limit is {limit} bytes",
                status_code=413,
            )


__all__ = [
    "MAX_COMMAND_ITEMS",
    "MAX_ENV_ITEMS",
    "MAX_EXCLUDE_PATTERNS",
    "MAX_JSON_BODY_BYTES",
    "MAX_LIST_RESULTS",
    "MAX_SEARCH_PATTERN_CHARS",
    "MAX_STDIN_CHARS",
    "MAX_TEXT_RESPONSE_BYTES",
    "MULTIPART_OVERHEAD_BYTES",
    "UPLOAD_CHUNK_BYTES",
    "SandboxLimitError",
    "SandboxLimits",
]
