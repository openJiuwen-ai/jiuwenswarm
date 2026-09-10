# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Distributed file-transfer config (Gateway ↔ AgentServer chunked transfer)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from jiuwenswarm.common.config import get_config

logger = logging.getLogger(__name__)


@dataclass
class FileTransferConfig:
    """文件传输配置模型.

    Attributes:
        enabled: 是否启用 Agent→Gateway 分块下载（解析后的有效值）
        chunk_size: 分片大小（字节），默认 64KB
        max_file_size: 最大文件大小（字节），默认 100MB，0=不限制
        transfer_timeout: 传输超时时间（秒），默认 300 秒
        received_files_dir: 接收文件存储目录，默认 "agent/jiuwenclaw_workspace/received_files"
        cleanup_interval: 临时文件清理间隔（秒），默认 3600 秒
        cleanup_age: 清理超过 N 秒的临时文件，默认 86400 秒（24小时）
        max_concurrent_transfers: 最大并发传输数，默认 5
    """

    enabled: bool = False
    chunk_size: int = 65536
    max_file_size: int = 104857600
    transfer_timeout: int = 300
    received_files_dir: str = "agent/jiuwenclaw_workspace/received_files"
    cleanup_interval: int = 3600
    cleanup_age: int = 86400
    max_concurrent_transfers: int = 5

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "FileTransferConfig":
        if not data:
            return cls()
        kwargs: dict[str, Any] = {
            "chunk_size": int(data.get("chunk_size", 65536)),
            "max_file_size": int(data.get("max_file_size", 104857600)),
            "transfer_timeout": int(data.get("transfer_timeout", 300)),
            "received_files_dir": str(
                data.get("received_files_dir", "agent/jiuwenclaw_workspace/received_files")
            ),
            "cleanup_interval": int(data.get("cleanup_interval", 3600)),
            "cleanup_age": int(data.get("cleanup_age", 86400)),
            "max_concurrent_transfers": int(data.get("max_concurrent_transfers", 5)),
        }
        if "enabled" in data:
            kwargs["enabled"] = bool(data.get("enabled"))
        return cls(**kwargs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "chunk_size": self.chunk_size,
            "max_file_size": self.max_file_size,
            "transfer_timeout": self.transfer_timeout,
            "received_files_dir": self.received_files_dir,
            "cleanup_interval": self.cleanup_interval,
            "cleanup_age": self.cleanup_age,
            "max_concurrent_transfers": self.max_concurrent_transfers,
        }


def resolve_file_transfer_enabled(ft_section: dict[str, Any] | None = None) -> bool:
    """Agent→Gateway 分块下载（``file.download.*`` push）是否启用.

    - 默认 **关闭**（个人与企业均关）。企业下载主路径为 OBS URL，不依赖 PushRegistry。
    - 若 YAML ``file_transfer.enabled`` **显式**写出，则以其为准（逃生 / 实验室可开）。
    - 若 env ``JIUWENSWARM_FILE_DOWNLOAD_VIA_PUSH`` 为真且 YAML 未显式写 ``enabled``，
      则视为逃生开启分块 push。
    - 上传仍由 ``is_enterprise()`` 走 MinIO，不经过本开关的 GW→Agent 分片。
    """
    import os

    section = ft_section
    if section is None:
        config = get_config()
        raw = config.get("file_transfer", {}) if isinstance(config, dict) else {}
        section = raw if isinstance(raw, dict) else {}
    if isinstance(section, dict) and "enabled" in section:
        return bool(section.get("enabled"))
    escape = os.getenv("JIUWENSWARM_FILE_DOWNLOAD_VIA_PUSH", "").strip().lower()
    if escape in {"1", "true", "yes", "on"}:
        return True
    return False


_file_transfer_config: FileTransferConfig | None = None


def get_file_transfer_config() -> FileTransferConfig:
    """获取文件传输配置（带缓存）."""
    global _file_transfer_config
    if _file_transfer_config is not None:
        return _file_transfer_config
    config = get_config()
    ft_config = config.get("file_transfer", {}) if isinstance(config, dict) else {}
    section = ft_config if isinstance(ft_config, dict) else {}
    loaded = FileTransferConfig.from_dict(section)
    loaded.enabled = resolve_file_transfer_enabled(section)
    _file_transfer_config = loaded
    logger.debug(
        "[FileTransferConfig] loaded enabled=%s chunk_size=%s",
        _file_transfer_config.enabled,
        _file_transfer_config.chunk_size,
    )
    return _file_transfer_config


def clear_file_transfer_config_cache() -> None:
    global _file_transfer_config
    _file_transfer_config = None
