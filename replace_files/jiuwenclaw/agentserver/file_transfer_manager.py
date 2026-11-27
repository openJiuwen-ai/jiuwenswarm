# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""FileTransferManager - AgentServer 端文件传输管理器.

负责：
1. 接收 Gateway 发送的文件（分片接收、组装、校验）
2. 发送文件到 Gateway（分片发送、进度追踪）
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import time
from pathlib import Path
from typing import Any

from jiuwenclaw.config import FileTransferConfig, get_file_transfer_config
from jiuwenclaw.e2a.constants import (
    FILE_DOWNLOAD_START,
    FILE_DOWNLOAD_CHUNK,
    FILE_DOWNLOAD_COMPLETE,
    FILE_TRANSFER_ERROR_SIZE_EXCEEDED,
    FILE_TRANSFER_ERROR_CHUNK_MISSING,
    FILE_TRANSFER_ERROR_CHECKSUM_MISMATCH,
)
from jiuwenclaw.utils import (
    get_service_root_dir,
    TransferProgress,
    safe_filename,
    guess_mime_type,
    FileTransferStartParams,
)


logger = logging.getLogger(__name__)


class FileTransferManager:
    """文件传输管理器（AgentServer 端）.

    用于分布式部署模式下，接收 Gateway 发送的文件，
    以及发送文件到 Gateway。
    """

    def __init__(self, config: FileTransferConfig | None = None) -> None:
        self._config = config or get_file_transfer_config()
        self._transfers: dict[str, TransferProgress] = {}
        self._lock = asyncio.Lock()
        # 接收文件存储目录（service 级别，多 agent 共享）
        self._received_dir = get_service_root_dir() / self._config.received_files_dir
        self._received_dir.mkdir(parents=True, exist_ok=True)
        # 后台清理任务
        self._cleanup_task: asyncio.Task | None = None
        self._running = False

    async def start_cleanup_task(self) -> None:
        """启动后台清理任务."""
        if self._cleanup_task is not None:
            return
        self._running = True
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("[FileTransferManager] 后台清理任务已启动")

    async def stop_cleanup_task(self) -> None:
        """停止后台清理任务."""
        self._running = False
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None
            logger.info("[FileTransferManager] 后台清理任务已停止")

    async def _cleanup_loop(self) -> None:
        """后台清理循环."""
        while self._running:
            try:
                await asyncio.sleep(self._config.cleanup_interval)
                cleaned = await self.cleanup_expired_transfers()
                if cleaned > 0:
                    logger.info("[FileTransferManager] 自动清理了 %d 个过期传输", cleaned)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("[FileTransferManager] 清理循环异常: %s", e)

    @property
    def config(self) -> FileTransferConfig:
        return self._config

    @property
    def enabled(self) -> bool:
        """是否启用分布式模式."""
        return self._config.enabled

    # =========================================================================
    # 接收文件（Gateway -> AgentServer）
    # =========================================================================

    async def handle_transfer_start(
        self,
        params: FileTransferStartParams,
    ) -> dict[str, Any]:
        """处理 FILE_TRANSFER_START 消息.

        Args:
            params: 文件传输开始参数

        Returns:
            响应字典，包含 accepted 或 error
        """
        async with self._lock:
            # 检查是否已存在
            if params.transfer_id in self._transfers:
                logger.warning(
                    "[FileTransferManager] transfer_id 已存在: %s",
                    params.transfer_id,
                )
                return {
                    "accepted": False,
                    "error": "transfer_id already exists",
                }

            # 检查文件大小
            if self._config.max_file_size > 0 and params.file_size > self._config.max_file_size:
                logger.warning(
                    "[FileTransferManager] 文件大小超限: %d > %d",
                    params.file_size,
                    self._config.max_file_size,
                )
                return {
                    "accepted": False,
                    "error": "file size exceeded",
                    "error_type": FILE_TRANSFER_ERROR_SIZE_EXCEEDED,
                }

            # 创建传输进度
            self._transfers[params.transfer_id] = TransferProgress(
                transfer_id=params.transfer_id,
                filename=params.filename,
                file_size=params.file_size,
                total_chunks=params.total_chunks,
                sha256=params.sha256,
                mime_type=params.mime_type,
                session_id=params.session_id,
            )

            logger.info(
                "[FileTransferManager] 开始接收文件: transfer_id=%s filename=%s size=%d chunks=%d",
                params.transfer_id,
                params.filename,
                params.file_size,
                params.total_chunks,
            )

            return {
                "accepted": True,
                "transfer_id": params.transfer_id,
            }

    async def handle_transfer_chunk(
        self,
        transfer_id: str,
        chunk_index: int,
        base64_data: str,
    ) -> dict[str, Any]:
        """处理 FILE_TRANSFER_CHUNK 消息.

        Args:
            transfer_id: 传输ID
            chunk_index: 分片索引
            base64_data: Base64 编码的分片数据

        Returns:
            响应字典
        """
        async with self._lock:
            progress = self._transfers.get(transfer_id)
            if progress is None:
                logger.warning(
                    "[FileTransferManager] 未知的 transfer_id: %s",
                    transfer_id,
                )
                return {
                    "accepted": False,
                    "error": "unknown transfer_id",
                }

            # 解码 Base64
            try:
                chunk_data = base64.b64decode(base64_data)
            except Exception as e:
                logger.warning(
                    "[FileTransferManager] Base64 解码失败: %s",
                    e,
                )
                return {
                    "accepted": False,
                    "error": f"base64 decode failed: {e}",
                }

            # 存储分片
            progress.chunks[chunk_index] = chunk_data
            progress.received_chunks += 1

            logger.debug(
                "[FileTransferManager] 收到分片: transfer_id=%s chunk=%d/%d",
                transfer_id,
                chunk_index + 1,
                progress.total_chunks,
            )

            return {
                "accepted": True,
                "transfer_id": transfer_id,
                "chunk_index": chunk_index,
            }

    async def handle_transfer_complete(
        self,
        transfer_id: str,
        sha256: str,
    ) -> dict[str, Any]:
        """处理 FILE_TRANSFER_COMPLETE 消息.

        组装文件、校验完整性、返回最终文件路径。

        Args:
            transfer_id: 传输ID
            sha256: 期望的 SHA256 校验值

        Returns:
            响应字典，包含成功/失败信息和文件路径
        """
        async with self._lock:
            progress = self._transfers.get(transfer_id)
            if progress is None:
                logger.warning(
                    "[FileTransferManager] 未知的 transfer_id: %s",
                    transfer_id,
                )
                return {
                    "success": False,
                    "error": "unknown transfer_id",
                }

            # 检查分片完整性
            if len(progress.chunks) != progress.total_chunks:
                logger.warning(
                    "[FileTransferManager] 分片缺失: %d/%d",
                    len(progress.chunks),
                    progress.total_chunks,
                )
                return {
                    "success": False,
                    "error": "chunks missing",
                    "error_type": FILE_TRANSFER_ERROR_CHUNK_MISSING,
                    "received": len(progress.chunks),
                    "expected": progress.total_chunks,
                }

            # 按序组装文件
            try:
                file_data = b"".join(
                    progress.chunks[i] for i in range(progress.total_chunks)
                )
            except KeyError as e:
                logger.warning(
                    "[FileTransferManager] 分片索引缺失: %s",
                    e,
                )
                return {
                    "success": False,
                    "error": f"chunk index missing: {e}",
                    "error_type": FILE_TRANSFER_ERROR_CHUNK_MISSING,
                }

            # 验证文件大小
            if len(file_data) != progress.file_size:
                logger.warning(
                    "[FileTransferManager] 文件大小不匹配: %d != %d",
                    len(file_data),
                    progress.file_size,
                )
                return {
                    "success": False,
                    "error": "file size mismatch",
                }

            # 计算 SHA256
            actual_sha256 = hashlib.sha256(file_data).hexdigest()
            if actual_sha256 != sha256:
                logger.warning(
                    "[FileTransferManager] SHA256 校验失败: %s != %s",
                    actual_sha256,
                    sha256,
                )
                return {
                    "success": False,
                    "error": "checksum mismatch",
                    "error_type": FILE_TRANSFER_ERROR_CHECKSUM_MISMATCH,
                    "expected": sha256,
                    "actual": actual_sha256,
                }

            # 生成存储路径
            safe_name = safe_filename(progress.filename)
            if progress.session_id:
                session_dir = self._received_dir / progress.session_id
                session_dir.mkdir(parents=True, exist_ok=True)
                file_path = session_dir / safe_name
            else:
                file_path = self._received_dir / f"{transfer_id}_{safe_name}"

            # 写入文件
            try:
                file_path.write_bytes(file_data)
            except Exception as e:
                logger.exception(
                    "[FileTransferManager] 写入文件失败: %s",
                    e,
                )
                return {
                    "success": False,
                    "error": f"write file failed: {e}",
                }

            # 清理传输进度
            del self._transfers[transfer_id]

            logger.info(
                "[FileTransferManager] 文件接收完成: transfer_id=%s path=%s size=%d",
                transfer_id,
                file_path,
                len(file_data),
            )

            return {
                "success": True,
                "file_path": str(file_path),
                "file_size": len(file_data),
                "sha256": actual_sha256,
            }

    # =========================================================================
    # 发送文件（AgentServer -> Gateway）
    # =========================================================================

    async def send_file(
        self,
        file_path: str | Path,
        send_callback: Any,
        session_id: str = "",
        channel_id: str = "",
        request_id: str = "",
    ) -> dict[str, Any]:
        """发送文件到 Gateway（分片发送）.

        Args:
            file_path: 文件路径
            send_callback: 发送消息的回调函数，签名为 async (event_type, params) -> None
            session_id: 会话ID
            channel_id: 频道ID
            request_id: 请求ID

        Returns:
            发送结果
        """
        file_path = Path(file_path)
        if not file_path.exists():
            return {
                "success": False,
                "error": f"file not found: {file_path}",
            }

        # 读取文件
        try:
            file_data = file_path.read_bytes()
        except Exception as e:
            return {
                "success": False,
                "error": f"read file failed: {e}",
            }

        file_size = len(file_data)
        filename = file_path.name
        sha256 = hashlib.sha256(file_data).hexdigest()
        chunk_size = self._config.chunk_size
        total_chunks = (file_size + chunk_size - 1) // chunk_size if chunk_size > 0 else 1

        # 检查文件大小
        if self._config.max_file_size > 0 and file_size > self._config.max_file_size:
            return {
                "success": False,
                "error": "file size exceeded",
                "error_type": FILE_TRANSFER_ERROR_SIZE_EXCEEDED,
            }

        transfer_id = f"dl_{int(time.time() * 1000)}_{hashlib.md5(filename.encode()).hexdigest()[:8]}"

        # 发送 FILE_DOWNLOAD_START
        await send_callback(
            FILE_DOWNLOAD_START,
            {
                "transfer_id": transfer_id,
                "filename": filename,
                "file_size": file_size,
                "sha256": sha256,
                "total_chunks": total_chunks,
                "chunk_size": chunk_size,
                "mime_type": guess_mime_type(filename),
            },
        )

        # 发送分片
        for i in range(total_chunks):
            start = i * chunk_size
            end = min(start + chunk_size, file_size)
            chunk_data = file_data[start:end]
            base64_data = base64.b64encode(chunk_data).decode("utf-8")

            await send_callback(
                FILE_DOWNLOAD_CHUNK,
                {
                    "transfer_id": transfer_id,
                    "chunk_index": i,
                    "base64_data": base64_data,
                    "chunk_size": len(chunk_data),
                },
            )

        # 发送 FILE_DOWNLOAD_COMPLETE
        await send_callback(
            FILE_DOWNLOAD_COMPLETE,
            {
                "transfer_id": transfer_id,
                "sha256": sha256,
                "total_chunks": total_chunks,
            },
        )

        logger.info(
            "[FileTransferManager] 文件发送完成: transfer_id=%s filename=%s size=%d",
            transfer_id,
            filename,
            file_size,
        )

        return {
            "success": True,
            "transfer_id": transfer_id,
            "file_size": file_size,
        }

    # =========================================================================
    # 工具方法
    # =========================================================================

    async def cleanup_expired_transfers(self) -> int:
        """清理过期的传输进度.

        Returns:
            清理的传输数量
        """
        async with self._lock:
            current_time = time.time()
            expired = []
            for transfer_id, progress in self._transfers.items():
                if current_time - progress.start_time > self._config.transfer_timeout:
                    expired.append(transfer_id)

            for transfer_id in expired:
                del self._transfers[transfer_id]
                logger.info(
                    "[FileTransferManager] 清理过期传输: %s",
                    transfer_id,
                )

            return len(expired)


# 单例
_file_transfer_manager: FileTransferManager | None = None


def get_file_transfer_manager() -> FileTransferManager:
    """获取文件传输管理器单例."""
    global _file_transfer_manager
    if _file_transfer_manager is None:
        _file_transfer_manager = FileTransferManager()
    return _file_transfer_manager


def clear_file_transfer_manager() -> None:
    """清除文件传输管理器单例."""
    global _file_transfer_manager
    _file_transfer_manager = None