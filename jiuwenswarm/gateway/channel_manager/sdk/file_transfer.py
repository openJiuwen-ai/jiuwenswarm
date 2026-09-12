# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unified file transfer service (E1 connector SDK).

Skeleton harvested from the three near-duplicate ``*_file_service.py``
implementations (feishu: 720 LOC, dingtalk: 481 LOC, wecom: 298 LOC).
The shared flow lives here; each platform only provides two small hooks
(upload / download) through the :class:`FileTransport` protocol.

Pattern: Template Method — the service owns size validation, retries,
filename safety and MIME detection; adapters own the platform API call.
"""

import asyncio
import logging
import mimetypes
import re
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

logger = logging.getLogger(__name__)

DEFAULT_MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB, common ceiling across platforms
DEFAULT_MAX_ATTEMPTS = 3


class FileTransferError(Exception):
    """Raised when a transfer fails after all retries."""


class FileTooLargeError(FileTransferError):
    """Raised before any network call when the file exceeds the channel limit."""


class FileTransport(Protocol):
    """The two platform-specific hooks each adapter must provide."""

    async def platform_upload(self, filename: str, content: bytes, mime_type: str) -> str:
        """Upload ``content`` and return a platform file id / key."""
        ...

    async def platform_download(self, file_key: str) -> bytes:
        """Download the file identified by ``file_key`` and return its bytes."""
        ...


@dataclass(frozen=True)
class TransferResult:
    """Outcome of a successful upload."""

    file_key: str
    filename: str
    size: int
    mime_type: str


class FileTransferService:
    """Shared upload/download flow for every channel adapter.

    Args:
        transport: the platform hooks (usually the adapter itself).
        max_file_size: hard size ceiling in bytes; checked before upload.
        max_attempts: total attempts per network operation (>= 1).
    """

    def __init__(
        self,
        transport: FileTransport,
        max_file_size: int = DEFAULT_MAX_FILE_SIZE,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        self._transport = transport
        self._max_file_size = max_file_size
        self._max_attempts = max(1, max_attempts)

    async def upload(self, filename: str, content: bytes) -> TransferResult:
        """Validate, sanitize and upload a file with retries."""
        if len(content) > self._max_file_size:
            raise FileTooLargeError(
                f"{filename}: {len(content)} bytes exceeds limit {self._max_file_size}"
            )
        safe_name = self.safe_filename(filename)
        mime_type = self.guess_mime_type(safe_name)

        async def _do_upload() -> str:
            return await self._transport.platform_upload(safe_name, content, mime_type)

        file_key = await self._with_retry(_do_upload, op=f"upload {safe_name}")
        return TransferResult(
            file_key=file_key, filename=safe_name, size=len(content), mime_type=mime_type
        )

    async def download(self, file_key: str) -> bytes:
        """Download a file with retries."""

        async def _do_download() -> bytes:
            return await self._transport.platform_download(file_key)

        return await self._with_retry(_do_download, op=f"download {file_key}")

    async def _with_retry(self, fn: Callable[[], Awaitable], op: str):
        """Linear backoff retry, harvested from the existing file services."""
        last_error: Exception | None = None
        for attempt in range(self._max_attempts):
            try:
                return await fn()
            except Exception as error:  # noqa: BLE001 - platform SDKs raise broadly
                last_error = error
                if attempt < self._max_attempts - 1:
                    delay = 1.0 * (attempt + 1)
                    logger.warning(
                        "%s failed (attempt %d/%d), retrying in %.1fs: %s",
                        op, attempt + 1, self._max_attempts, delay, error,
                    )
                    await asyncio.sleep(delay)
        raise FileTransferError(f"{op} failed after {self._max_attempts} attempts") from last_error

    @staticmethod
    def safe_filename(name: str) -> str:
        """Strip path components and characters unsafe on any platform."""
        name = name.replace("\\", "/").rsplit("/", 1)[-1]
        name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name).strip(" .")
        return name or "file"

    @staticmethod
    def guess_mime_type(filename: str) -> str:
        """Best-effort MIME type, defaulting to octet-stream."""
        mime, _ = mimetypes.guess_type(filename)
        return mime or "application/octet-stream"