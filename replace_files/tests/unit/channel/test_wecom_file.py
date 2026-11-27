# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Tests for WecomChannel file functionality."""

from unittest.mock import MagicMock, AsyncMock, patch
import pytest
from jiuwenclaw.channel.wecom_channel import WecomChannel, WecomConfig
from jiuwenclaw.channel.wecom_file_service import WecomFileService, detect_file_extension, get_mime_type
from jiuwenclaw.channel.base import RobotMessageRouter


# ---------------------------------------------------------------------------
# Test: File Extension Detection
# ---------------------------------------------------------------------------

def test_detect_file_extension_png():
    """Test PNG file detection."""
    # PNG 文件头：89 50 4E 47
    content = b'\x89PNG\r\n\x1a\n' + b'\x00' * 100
    assert detect_file_extension(content) == '.png'


def test_detect_file_extension_jpg():
    """Test JPG file detection."""
    # JPG 文件头：FF D8 FF
    content = b'\xff\xd8\xff\xe0' + b'\x00' * 100
    assert detect_file_extension(content) == '.jpg'


def test_detect_file_extension_mp4():
    """Test MP4 file detection."""
    # MP4 文件头：ftyp at offset 4
    content = b'\x00\x00\x00\x20ftypisom' + b'\x00' * 100
    assert detect_file_extension(content) == '.mp4'


def test_detect_file_extension_unknown():
    """Test unknown file detection."""
    content = b'unknown file content'
    assert detect_file_extension(content) == ''


# ---------------------------------------------------------------------------
# Test: MIME Type
# ---------------------------------------------------------------------------

def test_get_mime_type():
    """Test MIME type mapping."""
    assert get_mime_type('.png') == 'image/png'
    assert get_mime_type('.jpg') == 'image/jpeg'
    assert get_mime_type('.mp4') == 'video/mp4'
    assert get_mime_type('.pdf') == 'application/pdf'
    assert get_mime_type('.unknown') == 'application/octet-stream'


# ---------------------------------------------------------------------------
# Test: WecomFileService
# ---------------------------------------------------------------------------

def test_wecom_file_service_init():
    """Test WecomFileService initialization."""
    ws_client = MagicMock()
    service = WecomFileService(
        ws_client=ws_client,
        max_download_size=10 * 1024 * 1024,
        download_timeout=30,
        workspace_dir="/tmp/test",
    )
    
    assert service.max_download_size == 10 * 1024 * 1024
    assert service.download_timeout == 30
    assert service.workspace_dir == "/tmp/test"


def test_get_media_type_for_file():
    """Test media type detection for files."""
    ws_client = MagicMock()
    service = WecomFileService(ws_client=ws_client)
    
    # 图片
    assert service.get_media_type_for_file("/path/to/image.jpg") == 'image'
    assert service.get_media_type_for_file("/path/to/image.png") == 'image'
    
    # 语音
    assert service.get_media_type_for_file("/path/to/audio.mp3") == 'voice'
    assert service.get_media_type_for_file("/path/to/audio.wav") == 'voice'
    
    # 视频
    assert service.get_media_type_for_file("/path/to/video.mp4") == 'video'
    assert service.get_media_type_for_file("/path/to/video.mov") == 'video'
    
    # 其他文件
    assert service.get_media_type_for_file("/path/to/document.pdf") == 'file'
    assert service.get_media_type_for_file("/path/to/document.docx") == 'file'


# ---------------------------------------------------------------------------
# Test: WecomConfig
# ---------------------------------------------------------------------------

def test_wecom_config_defaults():
    """Test WecomConfig default values."""
    config = WecomConfig()
    
    assert config.enabled is False
    assert config.bot_id == ""
    assert config.secret == ""
    assert config.max_download_size == 100 * 1024 * 1024
    assert config.download_timeout == 60
    assert config.send_file_allowed is True
    assert config.enable_file_download is True
    assert config.workspace_dir == ""


# ---------------------------------------------------------------------------
# Test: WecomChannel File Handling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wecom_channel_file_service_initialization():
    """Test that WecomChannel initializes file service."""
    config = WecomConfig(
        enabled=True,
        bot_id="test_bot_id",
        secret="test_secret",
        workspace_dir="/tmp/test_workspace",
    )
    router = MagicMock(spec=RobotMessageRouter)
    
    with patch('jiuwenclaw.channel.wecom_channel.WECOM_AVAILABLE', True):
        with patch('jiuwenclaw.channel.wecom_channel.WSClient'):
            channel = WecomChannel(config, router)
            
            # 验证文件服务相关属性已初始化
            assert hasattr(channel, '_file_service')
            assert hasattr(channel, '_sent_file_paths_by_req')


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
