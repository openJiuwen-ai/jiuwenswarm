# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""分布式文件传输配置模型."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class FileTransferConfig:
    """分布式文件传输配置."""

    enabled: bool = False
    chunk_size: int = 65536
    max_file_size: int = 104857600
    transfer_timeout: int = 300
    max_retries: int = 3
    received_files_dir: str = "received_files"
    cleanup_interval: int = 3600
    cleanup_age: int = 86400
    max_concurrent_transfers: int = 5

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FileTransferConfig":
        """从字典创建配置实例."""
        if not data:
            return cls()
        return cls(
            enabled=bool(data.get("enabled", False)),
            chunk_size=int(data.get("chunk_size", 65536)),
            max_file_size=int(data.get("max_file_size", 104857600)),
            transfer_timeout=int(data.get("transfer_timeout", 300)),
            max_retries=int(data.get("max_retries", 3)),
            received_files_dir=str(data.get("received_files_dir", "received_files")),
            cleanup_interval=int(data.get("cleanup_interval", 3600)),
            cleanup_age=int(data.get("cleanup_age", 86400)),
            max_concurrent_transfers=int(data.get("max_concurrent_transfers", 5)),
        )

    def validate_file(self, filename: str, file_size: int) -> tuple[bool, str | None]:
        """
        验证文件是否符合配置限制.

        Returns:
            (is_valid, error_message)
        """
        if self.max_file_size > 0 and file_size > self.max_file_size:
            return False, f"文件大小超出限制：{file_size} > {self.max_file_size}"

        return True, None


_file_transfer_config: FileTransferConfig | None = None


def get_file_transfer_config() -> FileTransferConfig:
    """获取文件传输配置（单例模式）."""
    global _file_transfer_config
    if _file_transfer_config is None:
        # 延迟导入，避免循环导入
        from jiuwenclaw.config import get_config_raw

        try:
            cfg = get_config_raw()
            _file_transfer_config = FileTransferConfig.from_dict(
                cfg.get("file_transfer") or {}
            )
        except Exception:
            _file_transfer_config = FileTransferConfig()
    return _file_transfer_config


def reload_file_transfer_config() -> None:
    """重新加载文件传输配置."""
    global _file_transfer_config
    _file_transfer_config = None
    get_file_transfer_config()
