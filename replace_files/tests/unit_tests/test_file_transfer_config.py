# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""文件传输配置模型单元测试."""

import pytest

from jiuwenclaw.config import FileTransferConfig


# =========================================================================
# FileTransferConfig 测试（模块级函数，不需要实例）
# =========================================================================


def test_file_transfer_config_default_values():
    """测试默认值."""
    config = FileTransferConfig()
    assert config.enabled is False
    assert config.chunk_size == 65536
    assert config.max_file_size == 104857600
    assert config.transfer_timeout == 300
    assert config.max_retries == 3
    assert config.received_files_dir == "agent/workspace/received_files"
    assert config.cleanup_interval == 3600
    assert config.cleanup_age == 86400
    assert config.max_concurrent_transfers == 5


def test_file_transfer_config_from_dict_empty():
    """测试从空字典创建."""
    config = FileTransferConfig.from_dict(None)
    assert config.enabled is False

    config = FileTransferConfig.from_dict({})
    assert config.enabled is False


def test_file_transfer_config_from_dict_partial():
    """测试从部分字典创建."""
    config = FileTransferConfig.from_dict({
        "enabled": True,
        "chunk_size": 128000,
    })
    assert config.enabled is True
    assert config.chunk_size == 128000
    assert config.max_file_size == 104857600  # 默认值


def test_file_transfer_config_from_dict_full():
    """测试从完整字典创建."""
    data = {
        "enabled": True,
        "chunk_size": 128000,
        "max_file_size": 209715200,
        "transfer_timeout": 600,
        "max_retries": 5,
        "received_files_dir": "/tmp/received",
        "cleanup_interval": 7200,
        "cleanup_age": 172800,
        "max_concurrent_transfers": 10,
    }
    config = FileTransferConfig.from_dict(data)
    assert config.enabled is True
    assert config.chunk_size == 128000
    assert config.max_file_size == 209715200
    assert config.transfer_timeout == 600
    assert config.max_retries == 5
    assert config.received_files_dir == "/tmp/received"
    assert config.cleanup_interval == 7200
    assert config.cleanup_age == 172800
    assert config.max_concurrent_transfers == 10


def test_file_transfer_config_to_dict():
    """测试转换为字典."""
    config = FileTransferConfig(
        enabled=True,
        chunk_size=128000,
        max_file_size=209715200,
    )
    d = config.to_dict()
    assert d["enabled"] is True
    assert d["chunk_size"] == 128000
    assert d["max_file_size"] == 209715200
    assert "transfer_timeout" in d
    assert "max_retries" in d