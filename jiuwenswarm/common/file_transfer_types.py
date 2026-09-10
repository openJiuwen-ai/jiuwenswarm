# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Shared types/helpers for Gateway ↔ AgentServer file transfer."""

from __future__ import annotations

import mimetypes
import time
from dataclasses import dataclass, field

from jiuwenswarm.common.utils import get_service_root_dir

__all__ = [
    "FileTransferStartParams",
    "TransferProgress",
    "get_service_root_dir",
    "guess_mime_type",
    "safe_filename",
]


@dataclass
class FileTransferStartParams:
    """文件传输开始参数（用于封装多参数方法调用）."""

    transfer_id: str
    filename: str
    file_size: int
    sha256: str
    total_chunks: int
    chunk_size: int
    mime_type: str = ""
    session_id: str = ""
    channel_id: str = ""
    service_id: str = ""
    agent_id: str = ""


@dataclass
class TransferProgress:
    """文件传输进度状态（Gateway 和 AgentServer 共用）."""

    transfer_id: str
    filename: str
    file_size: int
    total_chunks: int
    received_chunks: int = 0
    sha256: str = ""
    chunks: dict[int, bytes] = field(default_factory=dict)
    start_time: float = field(default_factory=time.time)
    mime_type: str = ""
    session_id: str = ""
    channel_id: str = ""


def safe_filename(filename: str) -> str:
    """生成安全的文件名，防止路径遍历."""
    safe = filename.replace("/", "_").replace("\\", "_")
    safe = "".join(c for c in safe if c.isalnum() or c in "._- ")
    return safe or "unnamed_file"


def guess_mime_type(filename: str) -> str:
    mime_type, _ = mimetypes.guess_type(filename)
    return mime_type or "application/octet-stream"
