# pylint: disable=protected-access
"""Tests for FeishuChannel media upload (P1)."""

import json
import os
import tempfile
from unittest.mock import MagicMock, AsyncMock

import pytest

from jiuwenclaw.channel.feishu import FeishuChannel, FeishuConfig
from jiuwenclaw.channel.base import RobotMessageRouter
from jiuwenclaw.schema.message import Message, EventType


def _make_channel() -> FeishuChannel:
    """Create a FeishuChannel with mocked dependencies."""
    config = FeishuConfig(
        enabled=True,
        app_id="test_app_id",
        app_secret="test_app_secret",
        enable_file_upload=True,
    )
    router = MagicMock(spec=RobotMessageRouter)
    ch = FeishuChannel(config, router)
    ch._api_client = MagicMock()
    # Mock _file_service with async methods
    ch._file_service = MagicMock()
    ch._file_service.upload_image = AsyncMock()
    ch._file_service.upload_file_resource = AsyncMock()
    return ch


def _make_file_message(files: list[str], receive_id: str = "oc_test123") -> Message:
    """Create a chat.file Message."""
    return Message(
        id="req_001",
        type="event",
        channel_id="feishu",
        session_id="sess_001",
        params={},
        timestamp=1000.0,
        ok=True,
        payload={"event_type": "chat.file", "files": files},
        event_type=EventType.CHAT_FILE,
        metadata={"feishu_chat_id": receive_id},
    )


# ---------------------------------------------------------------------------
# _is_image_file
# ---------------------------------------------------------------------------

def test_is_image_jpg():
    ch = _make_channel()
    assert ch._is_image_file("/path/to/photo.jpg") is True


def test_is_image_jpeg():
    ch = _make_channel()
    assert ch._is_image_file("/path/to/photo.jpeg") is True


def test_is_image_png():
    ch = _make_channel()
    assert ch._is_image_file("/path/to/screenshot.png") is True


def test_is_image_gif():
    ch = _make_channel()
    assert ch._is_image_file("/path/to/anim.gif") is True


def test_is_image_bmp():
    ch = _make_channel()
    assert ch._is_image_file("/path/to/img.bmp") is True


def test_pdf_not_image():
    ch = _make_channel()
    assert ch._is_image_file("/path/to/doc.pdf") is False


def test_txt_not_image():
    ch = _make_channel()
    assert ch._is_image_file("/path/to/notes.txt") is False


def test_is_image_case_insensitive():
    ch = _make_channel()
    assert ch._is_image_file("/path/to/PHOTO.JPG") is True


# ---------------------------------------------------------------------------
# _upload_image
# ---------------------------------------------------------------------------

def test_upload_image_success():
    ch = _make_channel()
    mock_response = MagicMock()
    mock_response.success.return_value = True
    mock_response.data = MagicMock()
    mock_response.data.image_key = "img_v2_test_key"
    ch._api_client.im.v1.image.create.return_value = mock_response

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(b"fake png data")
        tmp_path = f.name

    try:
        result = ch._upload_image(tmp_path)
        assert result == "img_v2_test_key"
        ch._api_client.im.v1.image.create.assert_called_once()
    finally:
        os.unlink(tmp_path)


def test_upload_image_failure():
    ch = _make_channel()
    mock_response = MagicMock()
    mock_response.success.return_value = False
    mock_response.code = 99999
    mock_response.msg = "upload failed"
    ch._api_client.im.v1.image.create.return_value = mock_response

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(b"fake png data")
        tmp_path = f.name

    try:
        result = ch._upload_image(tmp_path)
        assert result is None
    finally:
        os.unlink(tmp_path)


def test_upload_image_file_not_found():
    ch = _make_channel()
    result = ch._upload_image("/nonexistent/path/image.png")
    assert result is None


# ---------------------------------------------------------------------------
# _upload_file
# ---------------------------------------------------------------------------

def test_upload_file_success():
    ch = _make_channel()
    mock_response = MagicMock()
    mock_response.success.return_value = True
    mock_response.data = MagicMock()
    mock_response.data.file_key = "file_v2_test_key"
    ch._api_client.im.v1.file.create.return_value = mock_response

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(b"fake pdf data")
        tmp_path = f.name

    try:
        result = ch._upload_file(tmp_path)
        assert result == "file_v2_test_key"
    finally:
        os.unlink(tmp_path)


def test_upload_file_failure():
    ch = _make_channel()
    mock_response = MagicMock()
    mock_response.success.return_value = False
    mock_response.code = 99999
    mock_response.msg = "upload failed"
    ch._api_client.im.v1.file.create.return_value = mock_response

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(b"fake pdf data")
        tmp_path = f.name

    try:
        result = ch._upload_file(tmp_path)
        assert result is None
    finally:
        os.unlink(tmp_path)


def test_upload_file_file_not_found():
    ch = _make_channel()
    result = ch._upload_file("/nonexistent/path/doc.pdf")
    assert result is None


# ---------------------------------------------------------------------------
# send() with chat.file event
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_image_file():
    ch = _make_channel()

    # Mock upload_image to return image_key
    ch._file_service.upload_image.return_value = {
        "image_key": "img_v2_abc",
        "file_type": "image",
    }

    # Mock message creation
    send_resp = MagicMock()
    send_resp.success.return_value = True
    ch._api_client.im.v1.message.create.return_value = send_resp

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(b"fake png")
        tmp_path = f.name

    try:
        msg = _make_file_message([tmp_path])
        await ch.send(msg)

        # Verify upload_image was called
        ch._file_service.upload_image.assert_called_once()
        # Verify message.create was called
        ch._api_client.im.v1.message.create.assert_called_once()
    finally:
        os.unlink(tmp_path)


@pytest.mark.asyncio
async def test_send_generic_file():
    ch = _make_channel()

    # Mock upload_file_resource to return file_key
    ch._file_service.upload_file_resource.return_value = {
        "file_key": "file_v2_xyz",
        "file_type": "stream",
    }

    # Mock message creation
    send_resp = MagicMock()
    send_resp.success.return_value = True
    ch._api_client.im.v1.message.create.return_value = send_resp

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(b"fake pdf")
        tmp_path = f.name

    try:
        msg = _make_file_message([tmp_path])
        await ch.send(msg)

        # Verify upload_file_resource was called
        ch._file_service.upload_file_resource.assert_called_once()
        # Verify message.create was called
        ch._api_client.im.v1.message.create.assert_called_once()
    finally:
        os.unlink(tmp_path)


@pytest.mark.asyncio
async def test_send_file_upload_failure_sends_text_fallback():
    ch = _make_channel()

    # Mock upload_image to return None (failure)
    ch._file_service.upload_image.return_value = None

    # Mock message creation for fallback
    send_resp = MagicMock()
    send_resp.success.return_value = True
    ch._api_client.im.v1.message.create.return_value = send_resp

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(b"fake png")
        tmp_path = f.name

    try:
        msg = _make_file_message([tmp_path])
        await ch.send(msg)

        # upload_image should have been called
        ch._file_service.upload_image.assert_called_once()
        # message.create should NOT be called since upload failed and no fallback
        ch._api_client.im.v1.message.create.assert_not_called()
    finally:
        os.unlink(tmp_path)
