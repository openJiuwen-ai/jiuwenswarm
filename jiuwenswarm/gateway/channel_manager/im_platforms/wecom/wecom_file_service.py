# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""WecomFileService - 企业微信文件服务

提供企业微信文件的下载和上传功能。
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path
from typing import Any

from loguru import logger

from jiuwenswarm.gateway.channel_manager.im_platforms.errors import (
    AttachmentPersistError,
)
from jiuwenswarm.server.runtime.attachments.upload_storage import (
    atomic_write_unique,
)
from jiuwenswarm.gateway.channel_manager.sdk.media_types import (
    detect_file_extension,
    get_mime_type,
)


class WecomFileService:
    """企业微信文件服务，处理文件下载和上传。"""

    def __init__(
        self,
        ws_client: Any,
        max_download_size: int = 100 * 1024 * 1024,
        download_timeout: int = 60,
        workspace_dir: str = "",
    ):
        """初始化文件服务。

        Args:
            ws_client: 企业微信 WebSocket 客户端（WSClient 实例）
            max_download_size: 最大下载文件大小（字节）
            download_timeout: 下载超时时间（秒）
            workspace_dir: 工作空间目录
        """
        self._ws_client = ws_client
        self.max_download_size = max_download_size
        self.download_timeout = download_timeout
        self.workspace_dir = workspace_dir
        self._download_semaphore = asyncio.Semaphore(3)
        # 附件落盘钩子（Phase 3）：Gateway 下载字节后经 E2A 交给目标 AgentServer 落盘。
        self._persist_hook: Any = None

    def set_persist_hook(self, hook: Any) -> None:
        """注入附件落盘钩子 ``async (content, category, filename) -> dict``。"""
        self._persist_hook = hook

    async def _persist_downloaded_file(
        self, content: bytes, category: str, filename: str,
    ) -> dict[str, Any]:
        """把下载字节落盘（经钩子到 AgentServer，或本地回落），返回 file_info 字段。"""
        if self._persist_hook is not None:
            try:
                return await self._persist_hook(content, category, filename)
            except AttachmentPersistError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise AttachmentPersistError(str(exc)) from exc
        download_dir = self._get_download_dir(category)
        # 本地回落（仅单用户 / 无 AgentServer 上下文）：用原子独占创建落盘，
        # 同名文件自动取 stem-N 后缀，并发下载与既有文件均不会被覆盖
        # （修复秒级时间戳重命名在同秒并发下仍会相互覆盖的 P1）。
        # 经钩子落盘（AgentServer 注入目录）时由 AgentServer 侧去重
        # （unique_upload_path），此处不重复处理。
        path = atomic_write_unique(Path(download_dir) / filename, content)
        return {"path": str(path), "name": path.name, "size": len(content)}

    def _get_download_dir(self, file_category: str) -> str:
        """获取下载目录路径。"""
        base_dir = os.path.join(self.workspace_dir, "wecom_files", "downloads", file_category)
        os.makedirs(base_dir, exist_ok=True)
        return base_dir

    @staticmethod
    def _safe_filename(name: str) -> str:
        """生成安全的文件名。"""
        # 移除或替换不安全字符
        safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)
        return safe[:100]  # 限制长度

    async def download_file(
        self,
        url: str,
        aes_key: str,
        message_id: str,
        file_category: str = "file",
        filename: str | None = None,
    ) -> dict | None:
        """下载文件并保存到本地。

        Args:
            url: 文件下载地址
            aes_key: AES 解密密钥（Base64 编码）
            message_id: 消息 ID（用于生成文件名）
            file_category: 文件类别（image/file/voice/video）
            filename: 原始文件名（可选）

        Returns:
            文件信息字典，失败返回 None
        """
        try:
            # 使用 SDK 的 download_file 方法（自动处理 AES 解密）
            async with self._download_semaphore:
                result = await asyncio.wait_for(
                    self._ws_client.download_file(url, aes_key),
                    timeout=self.download_timeout,
                )

            if not result or "buffer" not in result:
                logger.error("[WecomFileService] 下载文件失败：无返回数据")
                return None

            file_data = result["buffer"]
            original_filename = result.get("filename") or filename

            # 检查文件大小
            if len(file_data) > self.max_download_size:
                logger.warning(
                    f"[WecomFileService] 文件过大: {len(file_data)} > {self.max_download_size}"
                )
                return None

            # 检查空文件
            if len(file_data) == 0:
                logger.warning("[WecomFileService] 下载的文件为空")
                return None

            # 检测文件扩展名
            extension = detect_file_extension(file_data)
            if not extension and original_filename:
                # 从原始文件名提取扩展名
                _, ext = os.path.splitext(original_filename)
                if ext:
                    extension = ext

            # 生成文件名
            timestamp = int(time.time() * 1000)
            if not extension:
                extension = ".bin"
            
            if file_category == "file" and original_filename:
                # 普通文件保留原始文件名
                safe_name = self._safe_filename(original_filename)
                local_filename = f"{message_id}_{timestamp}_{safe_name}"
            else:
                # 图片/语音/视频使用时间戳命名
                local_filename = f"{message_id}_{timestamp}{extension}"

            # 保存文件
            persisted = await self._persist_downloaded_file(file_data, file_category, local_filename)
            file_path = persisted["path"]

            # 构建文件信息
            file_info = {
                "path": file_path,
                "name": original_filename or local_filename,
                "size": len(file_data),
                "mime_type": get_mime_type(extension),
                "file_category": file_category,
            }

            logger.info(
                f"[WecomFileService] 文件下载成功: {file_category}/{local_filename} "
                f"size={len(file_data)}"
            )
            return file_info

        except asyncio.TimeoutError:
            logger.error(f"[WecomFileService] 下载文件超时: {url}")
            return None
        except AttachmentPersistError:
            raise
        except Exception as e:
            logger.error(f"[WecomFileService] 下载文件失败: {e}")
            return None

    async def upload_file(
        self,
        file_path: str,
        media_type: str,
    ) -> str | None:
        """上传文件到企业微信。

        Args:
            file_path: 本地文件路径
            media_type: 媒体类型（file/image/voice/video）

        Returns:
            media_id，失败返回 None
        """
        try:
            # 读取文件
            with open(file_path, 'rb') as f:
                file_data = f.read()

            filename = os.path.basename(file_path)

            # 使用 SDK 的 upload_media 方法
            result = await self._ws_client.upload_media(
                file_data,
                type=media_type,
                filename=filename,
            )

            if not result or "media_id" not in result:
                logger.error("[WecomFileService] 上传文件失败：无 media_id 返回")
                return None

            media_id = result["media_id"]
            logger.info(
                f"[WecomFileService] 文件上传成功: {filename} -> {media_id}"
            )
            return media_id

        except Exception as e:
            logger.error(f"[WecomFileService] 上传文件失败: {e}")
            return None

    @classmethod
    def get_media_type_for_file(cls, file_path: str) -> str:
        """根据文件扩展名确定媒体类型。

        Args:
            file_path: 文件路径

        Returns:
            媒体类型（file/image/voice/video）
        """
        ext = os.path.splitext(file_path)[1].lower()
        
        # 图片
        if ext in {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'}:
            return 'image'
        
        # 语音
        if ext in {'.mp3', '.wav', '.aac', '.ogg', '.flac', '.m4a'}:
            return 'voice'
        
        # 视频
        if ext in {'.mp4', '.mov', '.avi', '.mkv', '.flv', '.webm'}:
            return 'video'
        
        # 其他文件
        return 'file'
