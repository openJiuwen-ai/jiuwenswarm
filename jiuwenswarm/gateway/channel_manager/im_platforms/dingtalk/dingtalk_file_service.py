# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""DingTalk File Service

提供钉钉文件的下载和上传功能。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import stat
from pathlib import Path
from typing import Any, Callable

import httpx
from loguru import logger

from jiuwenswarm.gateway.channel_manager.im_platforms.errors import (
    AttachmentPersistError,
)
from jiuwenswarm.gateway.embedded.runtime.attachments.upload_storage import (
    atomic_write_unique,
)


class DingTalkFileUploadError(RuntimeError):
    """A group-space file upload failed with a stable delivery classification."""

    def __init__(self, message: str, *, outcome: str = "failed", code: str = "DINGTALK_UPLOAD_FAILED") -> None:
        super().__init__(message)
        self.outcome = outcome
        self.code = code


# 文件魔数映射（用于格式检测）
FILE_SIGNATURES = {
    # 图片
    b'\x89PNG': '.png',
    b'\xff\xd8\xff': '.jpg',
    b'GIF8': '.gif',
    b'RIFF': '.webp',  # 需要进一步检查 WEBP 标识
    # 音频
    b'ID3': '.mp3',
    b'\xff\xfb': '.mp3',
    b'\xff\xfa': '.mp3',
    b'fLaC': '.flac',
    b'OggS': '.ogg',
    # 视频
    b'ftyp': '.mp4',
    b'moof': '.mp4',
    b'moov': '.mp4',
    b'\x1a\x45\xdf\xa3': '.mkv',
    b'FLV': '.flv',
}

# MIME 类型映射
MIME_TYPES = {
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.gif': 'image/gif',
    '.webp': 'image/webp',
    '.mp3': 'audio/mpeg',
    '.wav': 'audio/wav',
    '.ogg': 'audio/ogg',
    '.flac': 'audio/flac',
    '.mp4': 'video/mp4',
    '.mkv': 'video/x-matroska',
    '.flv': 'video/x-flv',
    '.pdf': 'application/pdf',
    '.doc': 'application/msword',
    '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    '.xls': 'application/vnd.ms-excel',
    '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    '.ppt': 'application/vnd.ms-powerpoint',
    '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    '.txt': 'text/plain',
    '.zip': 'application/zip',
    '.json': 'application/json',
}


def detect_file_extension(content: bytes) -> str:
    """通过文件头魔数检测文件扩展名。"""
    if len(content) < 12:
        return ''

    # 检查 WEBP（RIFF....WEBP）
    if content[:4] == b'RIFF' and content[8:12] == b'WEBP':
        return '.webp'

    # 检查 WAV（RIFF....WAVE）
    if content[:4] == b'RIFF' and content[8:12] == b'WAVE':
        return '.wav'

    # 检查 MP4（ftyp/moof/moov）
    if content[4:8] in (b'ftyp', b'moof', b'moov'):
        return '.mp4'

    # 检查其他格式
    for signature, ext in FILE_SIGNATURES.items():
        if content.startswith(signature):
            return ext

    return ''


def get_mime_type(extension: str) -> str:
    """获取文件扩展名对应的 MIME 类型。"""
    return MIME_TYPES.get(extension.lower(), 'application/octet-stream')


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
        signed_upload: Callable[[str, dict[str, str], bytes], Any] | None = None,
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
        self._signed_upload = signed_upload or self._upload_signed_url
        self._download_semaphore = asyncio.Semaphore(3)
        # 附件落盘钩子（Phase 3）：Gateway 下载字节后经 E2A 交给目标 AgentServer 落盘。
        self._persist_hook: Any = None

    async def _upload_signed_url(self, url: str, headers: dict[str, str], content: bytes) -> None:
        from urllib.parse import urlsplit

        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme != "https"
            or not host.endswith((".aliyuncs.com", ".alibabausercontent.com", ".dingtalk.com"))
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise DingTalkFileUploadError("DingTalk returned an unsupported upload URL")
        response = await self._http.put(url, content=content, headers=headers)
        if not response.is_success:
            raise DingTalkFileUploadError(
                f"DingTalk signed upload failed with HTTP {response.status_code}",
                outcome="unknown" if response.status_code >= 500 or response.status_code == 408 else "failed",
            )

    @staticmethod
    def _response_body(response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except (ValueError, TypeError):
            body = {}
        return body if isinstance(body, dict) else {}

    async def _post_group_api(
        self,
        url: str,
        *,
        token: str,
        json_body: dict[str, Any],
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            response = await self._http.post(
                url,
                json=json_body,
                params=params,
                headers={"x-acs-dingtalk-access-token": token},
            )
        except (httpx.ConnectTimeout, httpx.ConnectError, httpx.PoolTimeout) as exc:
            raise DingTalkFileUploadError(
                "DingTalk group file API connection failed", outcome="pending", code="DINGTALK_NOT_CONNECTED",
            ) from exc
        except (httpx.ReadTimeout, httpx.ReadError, httpx.WriteError) as exc:
            raise DingTalkFileUploadError(
                "DingTalk group file API outcome is unknown", outcome="unknown",
            ) from exc
        if response.status_code == 401:
            raise DingTalkFileUploadError(
                "DingTalk app token expired", outcome="pending", code="DINGTALK_TOKEN_EXPIRED",
            )
        if response.status_code >= 500 or response.status_code == 408:
            raise DingTalkFileUploadError(
                "DingTalk group file API outcome is unknown", outcome="unknown",
            )
        body = self._response_body(response)
        if response.status_code != 200 or body.get("code"):
            raise DingTalkFileUploadError(
                "DingTalk rejected the group file operation", code="DINGTALK_GROUP_UPLOAD_REJECTED",
            )
        return body

    async def upload_group_file(
        self,
        *,
        conversation_id: str,
        sender_staff_id: str,
        file_path: Path,
        access_token: str | None = None,
    ) -> dict[str, str]:
        """Upload an existing output file into the trusted current group's file space."""
        if not all(isinstance(value, str) and value.strip() for value in (conversation_id, sender_staff_id)):
            raise DingTalkFileUploadError("DingTalk group file route is incomplete", code="INVALID_DELIVERY_PAYLOAD")
        path = Path(file_path)
        try:
            before = path.lstat()
            if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
                raise DingTalkFileUploadError("DingTalk upload source is not a regular file", code="OUTPUT_FILE_INVALID")
            if before.st_size > self._max_download_size:
                raise DingTalkFileUploadError("DingTalk upload exceeds the configured file size limit")
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
            try:
                opened = os.fstat(descriptor)
                if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    raise DingTalkFileUploadError("DingTalk upload source changed while opening", code="OUTPUT_FILE_INVALID")
                chunks: list[bytes] = []
                remaining = before.st_size + 1
                while remaining:
                    chunk = os.read(descriptor, min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                content = b"".join(chunks)
            finally:
                os.close(descriptor)
        except DingTalkFileUploadError:
            raise
        except OSError as exc:
            raise DingTalkFileUploadError("DingTalk upload source is unavailable", code="OUTPUT_FILE_INVALID") from exc
        if len(content) != before.st_size or len(content) > self._max_download_size:
            raise DingTalkFileUploadError("DingTalk upload source changed while reading", code="OUTPUT_FILE_INVALID")

        token = access_token or await self._get_token()
        if not token:
            raise DingTalkFileUploadError(
                "DingTalk access token is unavailable", outcome="pending", code="DINGTALK_TOKEN_UNAVAILABLE",
            )
        name = self._safe_filename(path.name)
        if not name:
            raise DingTalkFileUploadError("DingTalk upload file name is invalid", code="OUTPUT_FILE_INVALID")

        try:
            identity_response = await self._http.post(
                f"{self._oapi_base}/topapi/v2/user/get",
                params={"access_token": token},
                json={"userid": sender_staff_id, "language": "zh_CN"},
            )
        except (httpx.ConnectTimeout, httpx.ConnectError, httpx.PoolTimeout, httpx.ReadTimeout) as exc:
            raise DingTalkFileUploadError(
                "DingTalk sender identity lookup failed", outcome="pending", code="DINGTALK_NOT_CONNECTED",
            ) from exc
        identity = self._response_body(identity_response)
        user = identity.get("result") if isinstance(identity.get("result"), dict) else identity
        union_id = user.get("unionid") if isinstance(user, dict) else None
        if identity_response.status_code == 401:
            raise DingTalkFileUploadError(
                "DingTalk app token expired", outcome="pending", code="DINGTALK_TOKEN_EXPIRED",
            )
        if identity_response.status_code >= 500 or identity_response.status_code == 408:
            raise DingTalkFileUploadError("DingTalk sender identity lookup outcome is unknown", outcome="unknown")
        if identity_response.status_code != 200 or identity.get("errcode") not in (None, 0, "0"):
            raise DingTalkFileUploadError("DingTalk could not resolve the current group sender")
        if not isinstance(union_id, str) or not union_id.strip():
            raise DingTalkFileUploadError("DingTalk sender identity has no unionId", code="DINGTALK_IDENTITY_UNAVAILABLE")

        group_space = await self._post_group_api(
            f"{self._api_base}/v1.0/convFile/conversations/spaces/query",
            token=token,
            params={"unionId": union_id},
            json_body={"openConversationId": conversation_id},
        )
        space = group_space.get("space") if isinstance(group_space.get("space"), dict) else {}
        space_id = space.get("spaceId")
        if not isinstance(space_id, (str, int)) or not str(space_id).strip():
            raise DingTalkFileUploadError("DingTalk did not return the current group's file space")
        space_id = str(space_id)
        root_uuid = space.get("rootDentryUuid") or space.get("rootFolderId")
        if isinstance(root_uuid, str) and root_uuid.strip():
            from urllib.parse import quote

            upload_url = f"{self._api_base}/v2.0/storage/spaces/files/{quote(root_uuid.strip(), safe='')}/uploadInfos/query"
            query = {"protocol": "HEADER_SIGNATURE", "option": {
                "storageDriver": "DINGTALK",
                "preCheckParam": {"size": len(content), "name": name},
            }}
            commit_url = f"{self._api_base}/v2.0/storage/spaces/files/{quote(root_uuid.strip(), safe='')}/commit"
            commit_body = {"name": name, "option": {"size": len(content), "conflictStrategy": "AUTO_RENAME"}}
        else:
            upload_url = f"{self._api_base}/v1.0/storage/spaces/{space_id}/files/uploadInfos/query"
            query = {"protocol": "HEADER_SIGNATURE", "multipart": False, "option": {
                "storageDriver": "DINGTALK",
                "preCheckParam": {
                    "md5": hashlib.md5(content).hexdigest(),
                    "size": len(content),
                    "parentId": "0",
                    "name": name,
                },
            }}
            commit_url = f"{self._api_base}/v1.0/storage/spaces/{space_id}/files/commit"
            commit_body = {"name": name, "parentId": "0", "option": {
                "size": len(content), "conflictStrategy": "AUTO_RENAME",
            }}
        upload_info = await self._post_group_api(upload_url, token=token, params={"unionId": union_id}, json_body=query)
        upload_key = upload_info.get("uploadKey")
        signature = upload_info.get("headerSignatureInfo")
        urls = signature.get("resourceUrls") if isinstance(signature, dict) else None
        headers = signature.get("headers") if isinstance(signature, dict) else None
        if (
            not isinstance(upload_key, str) or not upload_key.strip()
            or not isinstance(urls, list) or not urls or not isinstance(urls[0], str)
            or not isinstance(headers, dict)
            or any(not isinstance(key, str) or not isinstance(value, str) for key, value in headers.items())
        ):
            raise DingTalkFileUploadError("DingTalk did not return a usable upload signature", code="DINGTALK_UPLOAD_INFO_INVALID")
        try:
            await self._signed_upload(urls[0], headers, content)
        except DingTalkFileUploadError:
            raise
        except Exception as exc:
            raise DingTalkFileUploadError("DingTalk signed file upload failed", outcome="unknown") from exc
        committed = await self._post_group_api(
            commit_url,
            token=token,
            params={"unionId": union_id},
            json_body={"uploadKey": upload_key, **commit_body},
        )
        dentry = committed.get("dentry") if isinstance(committed.get("dentry"), dict) else {}
        dentry_id, dentry_uuid = dentry.get("id"), dentry.get("uuid")
        if dentry_id is None or not str(dentry_id).strip() or not isinstance(dentry_uuid, str) or not dentry_uuid.strip():
            raise DingTalkFileUploadError("DingTalk did not confirm the group file upload", outcome="unknown")
        return {"dentryId": str(dentry_id), "dentryUuid": dentry_uuid}

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
        max_retries: int = 3,
    ) -> bytes | None:
        """带重试的文件下载。

        Args:
            download_code: 文件下载码
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
        content = await self._download_with_retry(download_code)
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
        content = await self._download_with_retry(download_code)
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
        content = await self._download_with_retry(download_code)
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
        content = await self._download_with_retry(download_code)
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

