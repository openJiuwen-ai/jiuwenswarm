# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Tests for DingTalkChannel file upload and download functionality."""

import json
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock

import pytest
import httpx

from jiuwenclaw.channel.dingding import (
    DingTalkChannel,
    DingTalkConfig,
)
from jiuwenclaw.channel.dingtalk_file_service import DingTalkFileService
from jiuwenclaw.channel.base import RobotMessageRouter
from jiuwenclaw.schema.message import Message, EventType


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------

def _make_channel() -> DingTalkChannel:
    """Create a DingTalkChannel instance for testing."""
    config = DingTalkConfig(
        enabled=True,
        client_id="test_client_id",
        client_secret="test_client_secret",
        allow_from=["test_user"],
        enable_file_upload=True,
        enable_file_download=True,
        max_download_size=20 * 1024 * 1024,
        download_timeout=60,
        workspace_dir="",
    )
    router = MagicMock(spec=RobotMessageRouter)
    ch = DingTalkChannel(config, router)
    setattr(ch, '_http', MagicMock(spec=httpx.AsyncClient))
    setattr(ch, '_file_service', MagicMock(spec=DingTalkFileService))
    setattr(ch, '_access_token', "test_token")
    return ch


def _make_file_message(file_paths: list[str]) -> Message:
    """Create a file message for testing."""
    files = [{"path": path, "name": os.path.basename(path)} for path in file_paths]
    return Message(
        id="test_request_id",
        type="req",
        channel_id="dingtalk",
        session_id="test_session",
        params={},
        payload={"event_type": "chat.file", "files": files},
        metadata={
            "dingtalk_sender_id": "test_user",
            "dingtalk_chat_id": "test_chat",
            "conversation_type": "1",
            "conversation_id": "test_conv",
        },
        timestamp=0,
        ok=True,
        req_method="chat_send",
    )


# ---------------------------------------------------------------------------
# Test 1: File Upload Success
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_image_file_success():
    """Test successful image file upload and send."""
    ch = _make_channel()
    
    file_service = getattr(ch, '_file_service')
    http_client = getattr(ch, '_http')
    
    file_service.upload_media = AsyncMock(return_value="test_media_id_123")

    # Mock HTTP client response
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = '{"errcode": 0}'
    http_client.post = AsyncMock(return_value=mock_response)

    # Create temporary image file
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(b"\x89PNG\r\n\x1a\n")  # PNG magic number
        tmp_path = f.name

    try:
        msg = _make_file_message([tmp_path])
        await ch.send(msg)

        # Verify upload was called
        file_service.upload_media.assert_called_once()
        
        # Verify HTTP POST was called
        assert http_client.post.call_count == 3
        call_args = http_client.post.call_args
        assert call_args[0][0] == "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
        
        # Verify request body
        request_data = call_args[1]["json"]
        assert request_data["robotCode"] == "test_client_id"
        assert request_data["msgKey"] == "sampleImageMsg"
        
        msg_param = json.loads(request_data["msgParam"])
        assert msg_param["photoURL"] == "test_media_id_123"
        
    finally:
        os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# Test 2: File Download Success
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_picture_message_success():
    """Test successful picture message download."""
    ch = _make_channel()

    file_service = getattr(ch, '_file_service')
    file_service.download_image = AsyncMock(return_value={
    "path": "/tmp/test_image.png",
    "name": "test_image.png",
    "size": 1024,
    "mime_type": "image/png",
    "download_code": "test_download_code",
    "file_category": "image",
})

    raw_data = {
        "content": {
            "downloadCode": "test_download_code",
        },
        "msgId": "test_msg_id",
    }

    content, files = await ch.handle_picture_message(
        raw_data, "test_user", "test_conv", "1"
    )

    # Verify download was called
    file_service.download_image.assert_called_once_with(
        "test_download_code", "test_msg_id"
    )
    
    # Verify return values
    assert content == "[图片]"
    assert files is not None
    assert len(files) == 1
    assert files[0]["name"] == "test_image.png"
    assert files[0]["mime_type"] == "image/png"


# ---------------------------------------------------------------------------
# Test 3: File Download Disabled
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_picture_message_disabled():
    """Test picture message handling when download is disabled."""
    config = DingTalkConfig(
        enabled=True,
        client_id="test_client_id",
        client_secret="test_client_secret",
        allow_from=["test_user"],
        enable_file_download=False,  # Download disabled
    )
    router = MagicMock(spec=RobotMessageRouter)
    ch = DingTalkChannel(config, router)

    raw_data = {
        "content": {
            "downloadCode": "test_download_code",
        },
        "msgId": "test_msg_id",
    }

    content, files = await ch.handle_picture_message(
        raw_data, "test_user", "test_conv", "1"
    )

    # Verify no download was attempted
    assert content == "[图片: 文件下载功能已禁用]"
    assert files is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])