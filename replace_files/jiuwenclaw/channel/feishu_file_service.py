# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""飞书文件服务，负责文件的下载与上传。"""

import asyncio
import mimetypes
import os
import re
import time
from typing import Any

from jiuwenclaw.utils import logger

# 类型别名，用于类型提示
FeishuConfig = Any  # 避免循环导入

# ──────────────────────────────────────────────────────────────────────────────
# 飞书 file.create API 支持的 file_type 枚举
# https://open.feishu.cn/document/server-docs/im-v1/file/create
# ──────────────────────────────────────────────────────────────────────────────
_EXT_TO_FEISHU_FILE_TYPE: dict[str, str] = {
    # 音频 —— 飞书原生格式为 opus，其他音频作为 stream（可下载，不可在线播放）
    ".opus": "opus",
    ".mp3": "stream",
    ".wav": "stream",
    ".flac": "stream",
    ".ogg": "stream",
    ".aac": "stream",
    ".m4a": "stream",
    # 视频
    ".mp4": "mp4",
    ".mov": "mp4",
    ".avi": "mp4",
    ".mkv": "mp4",
    # 文档
    ".pdf": "pdf",
    ".doc": "doc",
    ".docx": "doc",
    ".xls": "xls",
    ".xlsx": "stream",
    ".ppt": "ppt",
    ".pptx": "stream",
}

# 图片扩展名集合（走 image.create 接口，不走 file.create）
_IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".svg"}
)

# 音频扩展名集合（可尝试以 audio msg_type 发送）
_AUDIO_EXTENSIONS: frozenset[str] = frozenset(
    {".opus", ".mp3", ".wav", ".flac", ".ogg", ".aac", ".m4a"}
)

# 视频扩展名集合（以 media msg_type 发送）
_VIDEO_EXTENSIONS: frozenset[str] = frozenset(
    {".mp4", ".mov", ".avi", ".mkv", ".flv", ".wmv", ".webm"}
)


def get_feishu_file_type(file_path: str) -> str:
    """根据文件扩展名返回飞书 file.create 所需的 file_type。"""
    ext = os.path.splitext(file_path)[1].lower()
    return _EXT_TO_FEISHU_FILE_TYPE.get(ext, "stream")


def is_image_file(file_path: str) -> bool:
    """判断是否为图片文件。"""
    ext = os.path.splitext(file_path)[1].lower()
    return ext in _IMAGE_EXTENSIONS


def is_audio_file(file_path: str) -> bool:
    """判断是否为音频文件。"""
    ext = os.path.splitext(file_path)[1].lower()
    return ext in _AUDIO_EXTENSIONS


def is_video_file(file_path: str) -> bool:
    """判断是否为视频文件。"""
    ext = os.path.splitext(file_path)[1].lower()
    return ext in _VIDEO_EXTENSIONS


class FeishuFileService:
    """
    飞书文件服务，负责文件的下载与上传。

    功能：
    - 从飞书下载用户发送的文件（图片/音频/视频/普通文件）
    - 上传文件到飞书用于发送
    """

    def __init__(
        self,
        api_client: Any,
        config: FeishuConfig,
        workspace_dir: str,
    ):
        """
        初始化文件服务。

        Args:
            api_client: 飞书 API 客户端（lark.Client 实例）
            config: 飞书通道配置（FeishuConfig）
            workspace_dir: 工作空间目录
        """
        self._api_client = api_client
        self._config = config
        self._workspace_dir = workspace_dir
        self._download_semaphore = asyncio.Semaphore(3)  # 限制并发下载数

    # ──────────────────────────────────────────────────────────────────────────
    # 工具方法
    # ──────────────────────────────────────────────────────────────────────────
    @classmethod
    def _ensure_dir(cls, path: str) -> None:
        """确保目录存在。"""
        os.makedirs(path, exist_ok=True)

    def _get_download_dir(self, file_type: str) -> str:
        """获取下载目录路径，并确保目录存在。"""
        base_dir = os.path.join(self._workspace_dir, "feishu_files", "downloads", file_type)
        self._ensure_dir(base_dir)
        return base_dir

    @classmethod
    def _generate_local_filename(
        cls,
        message_id: str,
        file_key: str,
        original_name: str = "",
        extension: str = "",
    ) -> str:
        """
        生成本地文件名，确保唯一性。

        格式: {message_id}_{safe_file_key}{ext}
        """
        if not extension and original_name:
            extension = os.path.splitext(original_name)[1]

        # 清理 file_key 中的特殊字符，取前 20 位
        safe_key = re.sub(r"[^\w\-]", "", file_key[:20])

        return f"{message_id}_{safe_key}{extension}"

    @classmethod
    def _guess_mime_type(cls, file_name: str) -> str:
        """根据文件名推断 MIME 类型。"""
        mime_type, _ = mimetypes.guess_type(file_name)
        return mime_type or "application/octet-stream"

    def _get_download_timeout(self) -> int:
        """获取下载超时时间（秒）。"""
        return getattr(self._config, "download_timeout", 60)

    # ──────────────────────────────────────────────────────────────────────────
    # 文件下载（核心工具）
    # ──────────────────────────────────────────────────────────────────────────

    async def _download_with_retry(
        self,
        download_func: Any,
        max_retries: int = 3,
    ) -> bytes | None:
        """
        带重试的文件下载（在线程池中执行同步 lark_oapi 调用）。

        Args:
            download_func: 无参可调用对象，执行后返回 lark_oapi response
            max_retries: 最大重试次数

        Returns:
            文件内容 bytes，失败返回 None
        """
        loop = asyncio.get_running_loop()
        timeout = self._get_download_timeout()

        for attempt in range(max_retries):
            try:
                # 在线程池执行同步 SDK 调用，并加超时控制
                response = await asyncio.wait_for(
                    loop.run_in_executor(None, download_func),
                    timeout=timeout,
                )

                if not response.success():
                    logger.warning(
                        "飞书文件下载失败 (尝试 %d/%d): code=%s msg=%s",
                        attempt + 1, max_retries,
                        response.code, response.msg,
                    )
                    if attempt < max_retries - 1:
                        await asyncio.sleep(1 * (attempt + 1))
                    continue

                # 兼容不同的响应结构（lark_oapi 各版本差异）
                file_content: bytes | None = None
                if hasattr(response, "file") and response.file:
                    file_content = response.file.read()
                elif hasattr(response, "data") and response.data:
                    if hasattr(response.data, "file") and response.data.file:
                        file_content = response.data.file.read()

                if file_content:
                    return file_content

                # 响应成功但内容为空——视为可重试的瞬时错误
                logger.warning(
                    "飞书文件下载响应中无文件内容 (尝试 %d/%d)，稍后重试",
                    attempt + 1, max_retries,
                )
                if attempt < max_retries - 1:
                    await asyncio.sleep(1 * (attempt + 1))

            except asyncio.TimeoutError:
                logger.warning(
                    "飞书文件下载超时 %ds (尝试 %d/%d)",
                    timeout, attempt + 1, max_retries,
                )
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 * (attempt + 1))
            except Exception as e:
                logger.error(
                    "飞书文件下载异常 (尝试 %d/%d): %s",
                    attempt + 1, max_retries, e,
                )
                if attempt < max_retries - 1:
                    await asyncio.sleep(1 * (attempt + 1))

        return None

    # ──────────────────────────────────────────────────────────────────────────
    # 各类型下载实现
    # ──────────────────────────────────────────────────────────────────────────

    async def _download_image_internal(
        self,
        image_key: str,
        message_id: str,
    ) -> dict | None:
        """
        下载图片文件。

        用户发送的图片必须通过 messageResource 接口下载（而非 image.get，
        后者仅支持应用自己上传的图片）。
        """
        try:
            from lark_oapi.api.im.v1 import GetMessageResourceRequest

            def _do_download():
                request = (
                    GetMessageResourceRequest.builder()
                    .message_id(message_id)
                    .file_key(image_key)
                    .type("image")
                    .build()
                )
                return self._api_client.im.v1.message_resource.get(request)

            file_content = await self._download_with_retry(_do_download)
            if not file_content:
                return None

            # 通过文件头魔数推断图片格式
            extension = self._detect_image_extension(file_content)

            local_name = self._generate_local_filename(
                message_id, image_key, extension=extension
            )
            download_dir = self._get_download_dir("images")
            file_path = os.path.join(download_dir, local_name)

            with open(file_path, "wb") as f:
                f.write(file_content)

            logger.info("飞书图片下载成功: %s", file_path)

            return {
                "path": file_path,
                "name": local_name,
                "size": len(file_content),
                "mime_type": self._guess_mime_type(local_name),
                "file_key": image_key,
                "file_category": "image",
            }

        except Exception as e:
            logger.error("下载飞书图片失败: %s", e)
            return None

    async def download_image(self, file_key: str, message_id: str) -> dict | None:
        """下载图片文件（公开接口）。"""
        return await self._download_image_internal(file_key, message_id)

    async def _download_file_internal(
        self,
        file_key: str,
        message_id: str,
        extra_info: dict[str, Any] | None = None,
    ) -> dict | None:
        """
        下载普通文件。

        用户发送的文件通过 messageResource 接口下载。
        """
        try:
            from lark_oapi.api.im.v1 import GetMessageResourceRequest

            extra_info = extra_info or {}
            original_name = extra_info.get("file_name", "")

            # 尝试下载文件，即使文件大小为0（可能是飞书端显示问题）
            def _do_download():
                request = (
                    GetMessageResourceRequest.builder()
                    .message_id(message_id)
                    .file_key(file_key)
                    .type("file")
                    .build()
                )
                return self._api_client.im.v1.message_resource.get(request)

            file_content = await self._download_with_retry(_do_download)
            if not file_content:
                return None

            extension = os.path.splitext(original_name)[1] if original_name else ""
            local_name = self._generate_local_filename(
                message_id, file_key, original_name, extension
            )
            download_dir = self._get_download_dir("files")
            file_path = os.path.join(download_dir, local_name)

            # 处理文件名冲突
            if os.path.exists(file_path):
                base, ext = os.path.splitext(file_path)
                file_path = f"{base}_{int(time.time())}{ext}"

            with open(file_path, "wb") as f:
                f.write(file_content)

            logger.info("飞书文件下载成功: %s", file_path)

            return {
                "path": file_path,
                "name": original_name or local_name,
                "size": len(file_content),
                "mime_type": self._guess_mime_type(original_name or local_name),
                "file_key": file_key,
                "file_category": "file",
            }

        except Exception as e:
            logger.error("下载飞书文件失败: %s", e)
            return None

    async def download_file_resource(
        self, file_key: str, message_id: str, extra_info: dict | None = None
    ) -> dict | None:
        """下载普通文件（公开接口）。"""
        return await self._download_file_internal(file_key, message_id, extra_info)

    async def _download_audio_internal(
        self,
        file_key: str,
        message_id: str,
    ) -> dict | None:
        """
        下载音频文件。

        飞书音频消息的原生格式为 Opus，messageResource 返回的也是 opus 编码数据。
        """
        try:
            from lark_oapi.api.im.v1 import GetMessageResourceRequest

            def _do_download():
                request = (
                    GetMessageResourceRequest.builder()
                    .message_id(message_id)
                    .file_key(file_key)
                    .type("audio")
                    .build()
                )
                return self._api_client.im.v1.message_resource.get(request)

            file_content = await self._download_with_retry(_do_download)
            if not file_content:
                return None

            # 飞书音频原生格式为 opus，通过文件头进一步确认
            extension = self._detect_audio_extension(file_content)

            local_name = self._generate_local_filename(
                message_id, file_key, extension=extension
            )
            download_dir = self._get_download_dir("audio")
            file_path = os.path.join(download_dir, local_name)

            with open(file_path, "wb") as f:
                f.write(file_content)

            logger.info("飞书音频下载成功: %s", file_path)

            return {
                "path": file_path,
                "name": local_name,
                "size": len(file_content),
                "mime_type": self._guess_mime_type(local_name),
                "file_key": file_key,
                "file_category": "audio",
            }

        except Exception as e:
            logger.error("下载飞书音频失败: %s", e)
            return None

    async def download_audio(self, file_key: str, message_id: str) -> dict | None:
        """下载音频文件（公开接口）。"""
        return await self._download_audio_internal(file_key, message_id)

    async def _download_media_internal(
        self,
        file_key: str,
        message_id: str,
    ) -> dict | None:
        """
        下载视频/媒体文件。

        飞书视频消息通过 messageResource 接口下载。
        """
        try:
            from lark_oapi.api.im.v1 import GetMessageResourceRequest

            def _do_download():
                request = (
                    GetMessageResourceRequest.builder()
                    .message_id(message_id)
                    .file_key(file_key)
                    .type("media")
                    .build()
                )
                return self._api_client.im.v1.message_resource.get(request)

            file_content = await self._download_with_retry(_do_download)
            if not file_content:
                return None

            extension = self._detect_video_extension(file_content)

            local_name = self._generate_local_filename(
                message_id, file_key, extension=extension
            )
            download_dir = self._get_download_dir("media")
            file_path = os.path.join(download_dir, local_name)

            with open(file_path, "wb") as f:
                f.write(file_content)

            logger.info("飞书视频下载成功: %s", file_path)

            return {
                "path": file_path,
                "name": local_name,
                "size": len(file_content),
                "mime_type": self._guess_mime_type(local_name),
                "file_key": file_key,
                "file_category": "media",
            }

        except Exception as e:
            logger.error("下载飞书视频失败: %s", e)
            return None

    async def download_media(self, file_key: str, message_id: str) -> dict | None:
        """下载视频文件（公开接口）。"""
        return await self._download_media_internal(file_key, message_id)

    # ──────────────────────────────────────────────────────────────────────────
    # 文件格式检测辅助方法
    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _detect_image_extension(data: bytes) -> str:
        """通过文件头魔数推断图片扩展名，默认 .png。"""
        if len(data) < 12:
            return ".png"
        header = data[:12]
        if header[:4] == b"\x89PNG":
            return ".png"
        if header[:2] == b"\xff\xd8":
            return ".jpg"
        if header[:6] in (b"GIF87a", b"GIF89a"):
            return ".gif"
        if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
            return ".webp"
        if header[:2] in (b"BM",):
            return ".bmp"
        return ".png"

    @staticmethod
    def _detect_audio_extension(data: bytes) -> str:
        """
        通过文件头推断音频扩展名。

        飞书音频原生格式为 Opus（OggOpus 容器），默认返回 .opus。
        """
        if len(data) < 12:
            return ".opus"
        header = data[:12]
        # OggS 容器（Opus/Vorbis）
        if header[:4] == b"OggS":
            return ".opus"
        # FLAC
        if header[:4] == b"fLaC":
            return ".flac"
        # WAVE
        if header[:4] == b"RIFF" and header[8:12] == b"WAVE":
            return ".wav"
        # MP3 (ID3 tag or sync frame)
        if header[:3] == b"ID3" or header[:2] == b"\xff\xfb":
            return ".mp3"
        # 默认为 opus（飞书原生格式）
        return ".opus"

    @staticmethod
    def _detect_video_extension(data: bytes) -> str:
        """通过文件头推断视频扩展名，默认 .mp4。"""
        if len(data) < 12:
            return ".mp4"
        header = data[:12]
        # MP4 / MOV (ftyp box)
        if header[4:8] in (b"ftyp", b"moof", b"moov"):
            return ".mp4"
        # MKV / WebM (EBML header)
        if header[:4] == b"\x1a\x45\xdf\xa3":
            return ".mkv"
        # FLV
        if header[:3] == b"FLV":
            return ".flv"
        # AVI (RIFF ... AVI )
        if header[:4] == b"RIFF" and header[8:12] == b"AVI ":
            return ".avi"
        return ".mp4"

    # ──────────────────────────────────────────────────────────────────────────
    # 文件上传
    # ──────────────────────────────────────────────────────────────────────────

    async def _upload_image_internal(self, file_path: str) -> dict | None:
        """
        上传图片到飞书（图片 API，失败时回退到文件 API）。

        飞书图片限制：20 MB。
        """
        try:
            from lark_oapi.api.im.v1 import CreateImageRequest, CreateImageRequestBody

            file_size = os.path.getsize(file_path)
            if file_size > 20 * 1024 * 1024:
                logger.error("图片超过飞书限制 20MB: %s (%d)", file_path, file_size)
                return None

            loop = asyncio.get_running_loop()

            def _do_upload():
                file_obj = open(file_path, "rb")
                try:
                    request = (
                        CreateImageRequest.builder()
                        .request_body(
                            CreateImageRequestBody.builder()
                            .image_type("message")
                            .image(file_obj)
                            .build()
                        )
                        .build()
                    )
                    return self._api_client.im.v1.image.create(request)
                finally:
                    file_obj.close()

            response = await loop.run_in_executor(None, _do_upload)

            if response.success():
                image_key = response.data.image_key
                logger.info("飞书图片上传成功: %s → %s", file_path, image_key)
                return {
                    "image_key": image_key,
                    "file_key": image_key,
                    "file_type": "image",
                }

            # 图片 API 失败，回退到文件 API
            logger.warning(
                "上传图片失败 (code=%s): %s，回退到文件 API", response.code, response.msg
            )
            return await self._upload_image_as_file(file_path)

        except Exception as e:
            logger.error("上传图片异常: %s", e)
            return None

    async def _upload_image_as_file(self, file_path: str) -> dict | None:
        """使用文件 API 上传图片（图片 API 失败时的回退方案）。"""
        try:
            from lark_oapi.api.im.v1 import CreateFileRequest, CreateFileRequestBody

            file_name = os.path.basename(file_path)
            loop = asyncio.get_running_loop()

            def _do_upload():
                file_obj = open(file_path, "rb")
                try:
                    request = (
                        CreateFileRequest.builder()
                        .request_body(
                            CreateFileRequestBody.builder()
                            .file_name(file_name)
                            .file_type("stream")
                            .file(file_obj)
                            .build()
                        )
                        .build()
                    )
                    return self._api_client.im.v1.file.create(request)
                finally:
                    file_obj.close()

            response = await loop.run_in_executor(None, _do_upload)

            if not response.success():
                logger.error(
                    "文件 API 上传图片失败: code=%s msg=%s", response.code, response.msg
                )
                return None

            file_key = response.data.file_key
            file_size = os.path.getsize(file_path)
            logger.info("飞书文件 API 上传图片成功: %s → %s", file_path, file_key)
            return {
                "file_key": file_key,
                "file_name": file_name,
                "file_size": file_size,
                "file_type": "file",
            }

        except Exception as e:
            logger.error("文件 API 上传图片异常: %s", e)
            return None

    async def upload_image(self, file_path: str) -> dict | None:
        """上传图片（公开接口）。"""
        return await self._upload_image_internal(file_path)

    async def _upload_file_internal(self, file_path: str) -> dict | None:
        """
        上传普通文件到飞书（file.create 接口）。

        飞书文件限制：30 MB。
        根据文件扩展名自动选择合适的 file_type（如 opus/mp4/pdf/doc/xls/ppt/stream）。
        """
        try:
            from lark_oapi.api.im.v1 import CreateFileRequest, CreateFileRequestBody

            file_size = os.path.getsize(file_path)
            if file_size > 30 * 1024 * 1024:
                logger.error("文件超过飞书限制 30MB: %s (%d)", file_path, file_size)
                return None

            file_name = os.path.basename(file_path)
            feishu_file_type = get_feishu_file_type(file_path)
            loop = asyncio.get_running_loop()

            def _do_upload():
                file_obj = open(file_path, "rb")
                try:
                    request = (
                        CreateFileRequest.builder()
                        .request_body(
                            CreateFileRequestBody.builder()
                            .file_name(file_name)
                            .file_type(feishu_file_type)
                            .file(file_obj)
                            .build()
                        )
                        .build()
                    )
                    return self._api_client.im.v1.file.create(request)
                finally:
                    file_obj.close()

            response = await loop.run_in_executor(None, _do_upload)

            if not response.success():
                logger.error("上传文件失败: code=%s msg=%s", response.code, response.msg)
                return None

            file_key = response.data.file_key
            logger.info(
                "飞书文件上传成功: %s → %s (file_type=%s)", file_path, file_key, feishu_file_type
            )
            return {
                "file_key": file_key,
                "file_name": file_name,
                "file_size": file_size,
                "file_type": feishu_file_type,
                "file_category": "audio" if is_audio_file(file_path) else (
                    "media" if is_video_file(file_path) else "file"
                ),
            }

        except Exception as e:
            logger.error("上传文件异常: %s", e)
            return None

    async def upload_file_resource(self, file_path: str) -> dict | None:
        """上传普通文件（公开接口）。"""
        return await self._upload_file_internal(file_path)
