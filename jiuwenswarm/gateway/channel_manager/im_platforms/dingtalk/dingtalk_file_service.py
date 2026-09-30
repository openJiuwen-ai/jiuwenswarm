# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""DingTalk File Service

提供钉钉文件的下载和上传功能。
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any, Callable

import httpx
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


class DingTalkFileService:
    """钉钉文件服务，处理文件下载和上传。"""

    def __init__(
        self,
        client_id: str,
        get_token_func: Callable[[], asyncio.coroutines.Coroutine[Any, Any, str | None]],
        http_client: httpx.AsyncClient,
        api_base: str,
        oapi_base: str,
        max_download_size: int = 100 * 1024 * 1024,
        download_timeout: int = 60,
        workspace_dir: str = "",
    ):
        """初始化文件服务。

        Args:
            client_id: 钉钉应用 client_id（robotCode）
            get_token_func: 获取 access_token 的异步函数
            http_client: HTTP 客户端
            max_download_size: 最大下载文件大小（字节）
            download_timeout: 下载超时时间（秒）
            workspace_dir: 工作空间目录
            api_base: 新版 v1.0 接口域名前缀
            oapi_base: 旧版 media 接口域名前缀
        """
        self._client_id = client_id
        self._get_token = get_token_func
        self._http = http_client
        self._max_download_size = max_download_size
        self._download_timeout = download_timeout
        self._workspace_dir = workspace_dir
        self._api_base = api_base
        self._oapi_base = oapi_base
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
        base_dir = os.path.join(self._workspace_dir, "dingtalk_files", "downloads", file_category)
        os.makedirs(base_dir, exist_ok=True)
        return base_dir

    @classmethod
    def _safe_filename(cls, name: str) -> str:
        """生成安全的文件名。"""
        # 移除或替换不安全字符
        safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)
        return safe[:100]  # 限制长度

    async def _download_with_retry(
        self,
        download_code: str,
        file_type: str,
        max_retries: int = 3,
    ) -> bytes | None:
        """带重试的文件下载。

        Args:
            download_code: 文件下载码
            file_type: 文件类型（image/file/voice/video）
            max_retries: 最大重试次数

        Returns:
            文件内容，失败返回 None
        """
        token = await self._get_token()
        if not token:
            logger.error("[DingTalkFileService] 无法获取 access_token")
            return None

        url = f"{self._api_base}/v1.0/robot/messageFiles/download"
        # 钉钉下载 API 使用 POST 方法，参数放在请求体中
        body = {
            "downloadCode": download_code,
            "robotCode": self._client_id,
        }
        headers = {
            "x-acs-dingtalk-access-token": token,
            "Content-Type": "application/json",
        }

        for attempt in range(max_retries):
            try:
                async with self._download_semaphore:
                    # 第一步：POST 请求获取 downloadUrl
                    response = await asyncio.wait_for(
                        self._http.post(url, json=body, headers=headers),
                        timeout=self._download_timeout,
                    )

                if response.status_code != 200:
                    logger.warning(
                        f"[DingTalkFileService] 获取下载链接失败 status={response.status_code} "
                        f"attempt={attempt + 1}/{max_retries} response={response.text}"
                    )
                    if attempt < max_retries - 1:
                        await asyncio.sleep(2 ** attempt)
                    continue

                # 解析响应获取 downloadUrl
                result = response.json()
                download_url = result.get("downloadUrl")
                if not download_url:
                    logger.warning(
                        f"[DingTalkFileService] 响应缺少 downloadUrl: {result}"
                    )
                    if attempt < max_retries - 1:
                        await asyncio.sleep(2 ** attempt)
                    continue

                # 第二步：GET 请求下载实际文件内容
                download_response = await asyncio.wait_for(
                    self._http.get(download_url),
                    timeout=self._download_timeout,
                )

                if download_response.status_code != 200:
                    logger.warning(
                        f"[DingTalkFileService] 下载文件失败 status={download_response.status_code} "
                        f"attempt={attempt + 1}/{max_retries}"
                    )
                    if attempt < max_retries - 1:
                        await asyncio.sleep(2 ** attempt)
                    continue

                content = download_response.content
                if not content:
                    logger.warning(
                        f"[DingTalkFileService] 下载内容为空 attempt={attempt + 1}/{max_retries}"
                    )
                    if attempt < max_retries - 1:
                        await asyncio.sleep(2 ** attempt)
                    continue

                if len(content) > self._max_download_size:
                    logger.warning(
                        f"[DingTalkFileService] 文件大小 {len(content)} 超过限制 {self._max_download_size}，跳过下载"
                    )
                    return None

                return content

            except asyncio.TimeoutError:
                logger.warning(
                    f"[DingTalkFileService] 下载超时 attempt={attempt + 1}/{max_retries}"
                )
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** (attempt + 1))
            except Exception as e:
                logger.error(f"[DingTalkFileService] 下载异常: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)

        return None

    async def download_image(self, download_code: str, message_id: str) -> dict | None:
        """下载图片文件。

        Args:
            download_code: 图片下载码
            message_id: 消息 ID

        Returns:
            文件信息字典，失败返回 None
        """
        content = await self._download_with_retry(download_code, "image")
        if not content:
            return None

        # 检测文件格式
        ext = detect_file_extension(content)
        if not ext:
            ext = '.png'  # 默认 PNG

        # 生成文件名
        safe_code = self._safe_filename(download_code[:20])
        filename = f"{message_id}_{safe_code}{ext}"

        # 保存文件
        try:
            persisted = await self._persist_downloaded_file(content, "images", filename)
            file_path = persisted["path"]

            return {
                "path": file_path,
                "name": persisted.get("name", filename),
                "size": len(content),
                "mime_type": get_mime_type(ext),
                "download_code": download_code,
                "file_category": "image",
            }
        except AttachmentPersistError:
            raise
        except Exception as e:
            logger.error(f"[DingTalkFileService] 保存图片失败: {e}")
            return None

    async def download_file(self, download_code: str, message_id: str, original_name: str = "") -> dict | None:
        """下载普通文件。

        Args:
            download_code: 文件下载码
            message_id: 消息 ID
            original_name: 原始文件名

        Returns:
            文件信息字典，失败返回 None
        """
        content = await self._download_with_retry(download_code, "file")
        if not content:
            return None

        # 确定扩展名
        if original_name:
            ext = os.path.splitext(original_name)[1].lower()
        else:
            ext = detect_file_extension(content) or '.bin'

        # 生成文件名
        safe_code = self._safe_filename(download_code[:20])
        if original_name:
            filename = self._safe_filename(original_name)
        else:
            filename = f"{message_id}_{safe_code}{ext}"

        # 保存文件
        try:
            persisted = await self._persist_downloaded_file(content, "files", filename)
            file_path = persisted["path"]

            return {
                "path": file_path,
                "name": persisted.get("name", filename),
                "size": len(content),
                "mime_type": get_mime_type(ext),
                "download_code": download_code,
                "file_category": "file",
            }
        except AttachmentPersistError:
            raise
        except Exception as e:
            logger.error(f"[DingTalkFileService] 保存文件失败: {e}")
            return None

    async def download_audio(self, download_code: str, message_id: str) -> dict | None:
        """下载音频文件。

        Args:
            download_code: 音频下载码
            message_id: 消息 ID

        Returns:
            文件信息字典，失败返回 None
        """
        content = await self._download_with_retry(download_code, "voice")
        if not content:
            return None

        # 检测文件格式
        ext = detect_file_extension(content)
        if not ext:
            ext = '.mp3'  # 默认 MP3

        # 生成文件名
        safe_code = self._safe_filename(download_code[:20])
        filename = f"{message_id}_{safe_code}{ext}"

        # 保存文件
        try:
            persisted = await self._persist_downloaded_file(content, "audio", filename)
            file_path = persisted["path"]

            return {
                "path": file_path,
                "name": persisted.get("name", filename),
                "size": len(content),
                "mime_type": get_mime_type(ext),
                "download_code": download_code,
                "file_category": "audio",
            }
        except AttachmentPersistError:
            raise
        except Exception as e:
            logger.error(f"[DingTalkFileService] 保存音频失败: {e}")
            return None

    async def download_video(self, download_code: str, message_id: str) -> dict | None:
        """下载视频文件。

        Args:
            download_code: 视频下载码
            message_id: 消息 ID

        Returns:
            文件信息字典，失败返回 None
        """
        content = await self._download_with_retry(download_code, "video")
        if not content:
            return None

        # 检测文件格式
        ext = detect_file_extension(content)
        if not ext:
            ext = '.mp4'  # 默认 MP4

        # 生成文件名
        safe_code = self._safe_filename(download_code[:20])
        filename = f"{message_id}_{safe_code}{ext}"

        # 保存文件
        try:
            persisted = await self._persist_downloaded_file(content, "video", filename)
            file_path = persisted["path"]

            return {
                "path": file_path,
                "name": persisted.get("name", filename),
                "size": len(content),
                "mime_type": get_mime_type(ext),
                "download_code": download_code,
                "file_category": "video",
            }
        except AttachmentPersistError:
            raise
        except Exception as e:
            logger.error(f"[DingTalkFileService] 保存视频失败: {e}")
            return None

    async def upload_media(self, file_path: str, file_type: str) -> str | None:
        """上传媒体文件到钉钉。

        Args:
            file_path: 本地文件路径
            file_type: 文件类型（image/file/voice/video）

        Returns:
            mediaId，失败返回 None
        """
        if not os.path.isfile(file_path):
            logger.warning(f"[DingTalkFileService] 文件不存在: {file_path}")
            return None

        token = await self._get_token()
        if not token:
            logger.error("[DingTalkFileService] 无法获取 access_token")
            return None

        # 使用钉钉旧版 API（与 dingtalk-stream SDK 一致）
        from urllib.parse import quote_plus
        url = f"{self._oapi_base}/media/upload?access_token={quote_plus(token)}"

        try:
            filename = os.path.basename(file_path)
            mime_type = get_mime_type(os.path.splitext(file_path)[1])
            with open(file_path, 'rb') as f:
                files = {
                    "media": (filename, f.read(), mime_type),
                }
                data = {
                    "type": file_type,
                }
                response = await self._http.post(url, data=data, files=files)

            if response.status_code != 200:
                logger.error(f"[DingTalkFileService] 上传失败: {response.text}")
                return None

            result = response.json()
            # 旧版 API 返回 media_id（下划线）
            media_id = result.get("media_id")
            if not media_id:
                logger.error(f"[DingTalkFileService] 上传响应缺少 media_id: {result}")
                return None

            logger.debug(f"[DingTalkFileService] 上传成功: {filename} -> {media_id}")
            return media_id

        except Exception as e:
            logger.error(f"[DingTalkFileService] 上传异常: {e}")
            return None

