# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import asyncio

import pytest

from jiuwenswarm.gateway.channel_manager.sdk import (
    FileTooLargeError,
    FileTransferError,
    FileTransferService,
)


class FakeTransport:
    """In-memory transport standing in for a platform SDK."""

    def __init__(self, fail_times: int = 0):
        self.fail_times = fail_times
        self.uploads: list[tuple[str, str]] = []

    async def platform_upload(self, filename, content, mime_type):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("transient platform error")
        self.uploads.append((filename, mime_type))
        return "key-123"

    async def platform_download(self, file_key):
        return b"data"


async def _instant_sleep(_delay):
    return None


async def test_upload_sanitizes_filename_and_guesses_mime():
    service = FileTransferService(FakeTransport())
    result = await service.upload("../evil dir\\rep ort.PDF", b"x")
    assert result.file_key == "key-123"
    assert "/" not in result.filename and "\\" not in result.filename
    assert result.mime_type == "application/pdf"


async def test_upload_rejects_oversized_file_before_any_network_call():
    transport = FakeTransport()
    service = FileTransferService(transport, max_file_size=10)
    with pytest.raises(FileTooLargeError):
        await service.upload("big.bin", b"x" * 11)
    assert transport.uploads == []


async def test_upload_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)
    service = FileTransferService(FakeTransport(fail_times=2), max_attempts=3)
    result = await service.upload("a.txt", b"x")
    assert result.file_key == "key-123"


async def test_upload_fails_after_max_attempts(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)
    service = FileTransferService(FakeTransport(fail_times=5), max_attempts=3)
    with pytest.raises(FileTransferError):
        await service.upload("a.txt", b"x")


async def test_download_returns_platform_bytes():
    service = FileTransferService(FakeTransport())
    assert await service.download("key-123") == b"data"