# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""FileTransferManager 单元测试."""

import asyncio
import base64
import hashlib
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jiuwenclaw.config import FileTransferConfig
from jiuwenclaw.agentserver.file_transfer_manager import (
    FileTransferManager,
    TransferProgress,
    get_file_transfer_manager,
    clear_file_transfer_manager,
)
from jiuwenclaw.utils import FileTransferStartParams


def test_transfer_progress_default_values():
    progress = TransferProgress(
        transfer_id="test-123",
        filename="test.txt",
        file_size=100,
        total_chunks=1,
    )
    assert progress.transfer_id == "test-123"
    assert progress.filename == "test.txt"
    assert progress.file_size == 100
    assert progress.total_chunks == 1
    assert progress.received_chunks == 0
    assert progress.chunks == {}


@pytest.fixture
def manager(tmp_path):
    config = FileTransferConfig(
        enabled=True,
        chunk_size=100,
        max_file_size=1000,
        received_files_dir=str(tmp_path / "received"),
    )
    return FileTransferManager(config)


def test_handle_transfer_start(manager):
    async def _test():
        params = FileTransferStartParams(
            transfer_id="test-1",
            filename="test.txt",
            file_size=200,
            sha256="abc123",
            total_chunks=2,
            chunk_size=100,
        )
        result = await manager.handle_transfer_start(params)
        assert result["accepted"] is True
        assert result["transfer_id"] == "test-1"
    asyncio.run(_test())


def test_handle_transfer_start_size_exceeded(manager):
    async def _test():
        params = FileTransferStartParams(
            transfer_id="test-2",
            filename="large.bin",
            file_size=2000,
            sha256="abc123",
            total_chunks=20,
            chunk_size=100,
        )
        result = await manager.handle_transfer_start(params)
        assert result["accepted"] is False
        assert "size exceeded" in result["error"]
    asyncio.run(_test())


def test_handle_transfer_start_duplicate(manager):
    async def _test():
        params1 = FileTransferStartParams(
            transfer_id="test-3",
            filename="test.txt",
            file_size=100,
            sha256="abc123",
            total_chunks=1,
            chunk_size=100,
        )
        await manager.handle_transfer_start(params1)
        params2 = FileTransferStartParams(
            transfer_id="test-3",
            filename="test2.txt",
            file_size=100,
            sha256="def456",
            total_chunks=1,
            chunk_size=100,
        )
        result = await manager.handle_transfer_start(params2)
        assert result["accepted"] is False
        assert "already exists" in result["error"]
    asyncio.run(_test())


def test_handle_transfer_chunk(manager):
    async def _test():
        params = FileTransferStartParams(
            transfer_id="test-4",
            filename="test.txt",
            file_size=100,
            sha256="abc123",
            total_chunks=1,
            chunk_size=100,
        )
        await manager.handle_transfer_start(params)

        data = b"Hello, World!"
        b64_data = base64.b64encode(data).decode("utf-8")
        result = await manager.handle_transfer_chunk(
            transfer_id="test-4",
            chunk_index=0,
            base64_data=b64_data,
        )
        assert result["accepted"] is True
        assert result["chunk_index"] == 0
    asyncio.run(_test())


def test_handle_transfer_chunk_unknown_transfer(manager):
    async def _test():
        result = await manager.handle_transfer_chunk(
            transfer_id="unknown",
            chunk_index=0,
            base64_data="SGVsbG8=",
        )
        assert result["accepted"] is False
        assert "unknown" in result["error"]
    asyncio.run(_test())


def test_handle_transfer_complete(manager):
    async def _test():
        file_data = b"Hello, World! This is a test file."
        sha256 = hashlib.sha256(file_data).hexdigest()
        chunk_size = manager.config.chunk_size
        total_chunks = (len(file_data) + chunk_size - 1) // chunk_size

        params = FileTransferStartParams(
            transfer_id="test-5",
            filename="test.txt",
            file_size=len(file_data),
            sha256=sha256,
            total_chunks=total_chunks,
            chunk_size=chunk_size,
        )
        await manager.handle_transfer_start(params)

        for i in range(total_chunks):
            start = i * chunk_size
            end = min(start + chunk_size, len(file_data))
            chunk_data = file_data[start:end]
            b64_data = base64.b64encode(chunk_data).decode("utf-8")
            await manager.handle_transfer_chunk(
                transfer_id="test-5",
                chunk_index=i,
                base64_data=b64_data,
            )

        result = await manager.handle_transfer_complete(
            transfer_id="test-5",
            sha256=sha256,
        )
        assert result["success"] is True
        assert "file_path" in result
        assert Path(result["file_path"]).exists()
        assert Path(result["file_path"]).read_bytes() == file_data
    asyncio.run(_test())


def test_handle_transfer_complete_checksum_mismatch(manager):
    async def _test():
        file_data = b"Test data"
        sha256 = hashlib.sha256(file_data).hexdigest()
        wrong_sha256 = "0" * 64

        params = FileTransferStartParams(
            transfer_id="test-6",
            filename="test.txt",
            file_size=len(file_data),
            sha256=sha256,
            total_chunks=1,
            chunk_size=100,
        )
        await manager.handle_transfer_start(params)

        b64_data = base64.b64encode(file_data).decode("utf-8")
        await manager.handle_transfer_chunk(
            transfer_id="test-6",
            chunk_index=0,
            base64_data=b64_data,
        )

        result = await manager.handle_transfer_complete(
            transfer_id="test-6",
            sha256=wrong_sha256,
        )
        assert result["success"] is False
        assert "checksum" in result["error"].lower()
    asyncio.run(_test())


def test_handle_transfer_complete_missing_chunks(manager):
    async def _test():
        file_data = b"Test data for missing chunks test"
        sha256 = hashlib.sha256(file_data).hexdigest()

        params = FileTransferStartParams(
            transfer_id="test-7",
            filename="test.txt",
            file_size=len(file_data),
            sha256=sha256,
            total_chunks=3,
            chunk_size=20,
        )
        await manager.handle_transfer_start(params)

        b64_data = base64.b64encode(file_data[:20]).decode("utf-8")
        await manager.handle_transfer_chunk(
            transfer_id="test-7",
            chunk_index=0,
            base64_data=b64_data,
        )

        result = await manager.handle_transfer_complete(
            transfer_id="test-7",
            sha256=sha256,
        )
        assert result["success"] is False
        assert "missing" in result["error"].lower()
    asyncio.run(_test())


def test_get_file_transfer_manager():
    clear_file_transfer_manager()
    manager1 = get_file_transfer_manager()
    manager2 = get_file_transfer_manager()
    assert manager1 is manager2


def test_clear_file_transfer_manager():
    clear_file_transfer_manager()
    manager1 = get_file_transfer_manager()
    clear_file_transfer_manager()
    manager2 = get_file_transfer_manager()
    assert manager1 is not manager2