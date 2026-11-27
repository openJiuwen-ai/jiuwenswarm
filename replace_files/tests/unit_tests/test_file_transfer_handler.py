# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""FileTransferHandler 单元测试."""

import asyncio
import base64
import hashlib
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenclaw.config import FileTransferConfig
from jiuwenclaw.gateway.file_transfer_handler import (
    FileTransferHandler,
    get_file_transfer_handler,
    clear_file_transfer_handler,
)
from jiuwenclaw.utils import safe_filename, guess_mime_type, FileTransferStartParams


@pytest.fixture
def handler(tmp_path):
    config = FileTransferConfig(
        enabled=True,
        chunk_size=100,
        max_file_size=1000,
        received_files_dir=str(tmp_path / "received"),
    )
    return FileTransferHandler(config)


def test_handle_download_start(handler):
    async def _test():
        params = FileTransferStartParams(
            transfer_id="dl-1",
            filename="test.txt",
            file_size=200,
            sha256="abc123",
            total_chunks=2,
            chunk_size=100,
        )
        result = await handler.handle_download_start(params)
        assert result["accepted"] is True
        assert result["transfer_id"] == "dl-1"
    asyncio.run(_test())


def test_handle_download_start_duplicate(handler):
    async def _test():
        params1 = FileTransferStartParams(
            transfer_id="dl-2",
            filename="test.txt",
            file_size=100,
            sha256="abc123",
            total_chunks=1,
            chunk_size=100,
        )
        await handler.handle_download_start(params1)
        params2 = FileTransferStartParams(
            transfer_id="dl-2",
            filename="test2.txt",
            file_size=100,
            sha256="def456",
            total_chunks=1,
            chunk_size=100,
        )
        result = await handler.handle_download_start(params2)
        assert result["accepted"] is False
        assert "already exists" in result["error"]
    asyncio.run(_test())


def test_handle_download_chunk(handler):
    async def _test():
        params = FileTransferStartParams(
            transfer_id="dl-3",
            filename="test.txt",
            file_size=100,
            sha256="abc123",
            total_chunks=1,
            chunk_size=100,
        )
        await handler.handle_download_start(params)

        data = b"Hello, World!"
        b64_data = base64.b64encode(data).decode("utf-8")
        result = await handler.handle_download_chunk(
            transfer_id="dl-3",
            chunk_index=0,
            base64_data=b64_data,
        )
        assert result["accepted"] is True
        assert result["chunk_index"] == 0
    asyncio.run(_test())


def test_handle_download_complete(handler):
    async def _test():
        file_data = b"Hello, World! This is a test file for download."
        sha256 = hashlib.sha256(file_data).hexdigest()
        chunk_size = handler.config.chunk_size
        total_chunks = (len(file_data) + chunk_size - 1) // chunk_size

        params = FileTransferStartParams(
            transfer_id="dl-4",
            filename="test.txt",
            file_size=len(file_data),
            sha256=sha256,
            total_chunks=total_chunks,
            chunk_size=chunk_size,
            session_id="sess-123",
            channel_id="web",
        )
        await handler.handle_download_start(params)

        for i in range(total_chunks):
            start = i * chunk_size
            end = min(start + chunk_size, len(file_data))
            chunk_data = file_data[start:end]
            b64_data = base64.b64encode(chunk_data).decode("utf-8")
            await handler.handle_download_chunk(
                transfer_id="dl-4",
                chunk_index=i,
                base64_data=b64_data,
            )

        result = await handler.handle_download_complete(
            transfer_id="dl-4",
            sha256=sha256,
        )
        assert result["success"] is True
        assert "file_path" in result
        assert Path(result["file_path"]).exists()
        assert Path(result["file_path"]).read_bytes() == file_data
        assert result["session_id"] == "sess-123"
        assert result["channel_id"] == "web"
    asyncio.run(_test())


def test_handle_download_complete_checksum_mismatch(handler):
    async def _test():
        file_data = b"Test data"
        sha256 = hashlib.sha256(file_data).hexdigest()
        wrong_sha256 = "0" * 64

        params = FileTransferStartParams(
            transfer_id="dl-5",
            filename="test.txt",
            file_size=len(file_data),
            sha256=sha256,
            total_chunks=1,
            chunk_size=100,
        )
        await handler.handle_download_start(params)

        b64_data = base64.b64encode(file_data).decode("utf-8")
        await handler.handle_download_chunk(
            transfer_id="dl-5",
            chunk_index=0,
            base64_data=b64_data,
        )

        result = await handler.handle_download_complete(
            transfer_id="dl-5",
            sha256=wrong_sha256,
        )
        assert result["success"] is False
        assert "checksum" in result["error"].lower()
    asyncio.run(_test())


def test_send_file_to_agent_server(handler, tmp_path):
    async def _test():
        test_file = tmp_path / "test_send.txt"
        test_data = b"Test file content for sending"
        test_file.write_bytes(test_data)

        responses = []

        async def mock_send_callback(method: str, params: dict):
            responses.append((method, params))
            if "start" in method:
                return {"accepted": True, "transfer_id": params["transfer_id"]}
            elif "chunk" in method:
                return {"accepted": True}
            elif "complete" in method:
                return {
                    "success": True,
                    "file_path": f"/received/{params['transfer_id']}_{test_file.name}",
                }
            return {"accepted": True}

        result = await handler.send_file_to_agent_server(
            file_path=str(test_file),
            send_callback=mock_send_callback,
            session_id="sess-123",
            channel_id="web",
        )

        assert result["success"] is True
        assert len(responses) >= 3

        start_calls = [r for r in responses if "start" in r[0]]
        assert len(start_calls) >= 1
    asyncio.run(_test())


def test_send_file_not_found(handler):
    async def _test():
        async def mock_send_callback(method: str, params: dict):
            return {"accepted": True}

        result = await handler.send_file_to_agent_server(
            file_path="/nonexistent/file.txt",
            send_callback=mock_send_callback,
        )

        assert result["success"] is False
        assert "not found" in result["error"].lower()
    asyncio.run(_test())


def test_get_file_transfer_handler():
    clear_file_transfer_handler()
    handler1 = get_file_transfer_handler()
    handler2 = get_file_transfer_handler()
    assert handler1 is handler2


def test_clear_file_transfer_handler():
    clear_file_transfer_handler()
    handler1 = get_file_transfer_handler()
    clear_file_transfer_handler()
    handler2 = get_file_transfer_handler()
    assert handler1 is not handler2


def test_safe_filename():
    assert safe_filename("test.txt") == "test.txt"
    assert "/" not in safe_filename("path/to/file.txt")
    assert "\\" not in safe_filename("path\\to\\file.txt")
    assert safe_filename("") == "unnamed_file"


def test_guess_mime_type():
    assert guess_mime_type("test.pdf") == "application/pdf"
    assert guess_mime_type("test.png") == "image/png"
    assert guess_mime_type("test.jpg") == "image/jpeg"
    assert guess_mime_type("test.unknown") == "application/octet-stream"