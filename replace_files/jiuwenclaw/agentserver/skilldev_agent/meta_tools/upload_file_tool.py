# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Upload file meta-tool: uploads a file from workspace to OBS and returns the CDN URL."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from openjiuwen.core.foundation.tool import LocalFunction, ToolCard

from jiuwenclaw.agentserver.skilldev.utils import create_upload_file_obs
from jiuwenclaw.agentserver.skilldev.utils.path_utils import resolve_and_validate_path

logger = logging.getLogger(__name__)


@dataclass
class UploadFileSuccessOutput:
    """上传文件成功时的出参。"""

    url: str
    name: str
    size_bytes: str
    mime: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": True,
            "url": self.url,
            "name": self.name,
            "sizeBytes": self.size_bytes,
            "mime": self.mime,
        }


@dataclass
class UploadFileErrorOutput:
    """上传文件失败时的出参。"""

    error: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": False,
            "error": self.error,
        }


_UPLOAD_FILE_TOOL_CARD = ToolCard(
    id="skilldev_upload_file_tool",
    name="upload_file",
    description=(
        "将工作区内的本地文件上传到对象存储，返回可下载的 obsUrl。"
        "当本轮任务的最终产物是一个文件（文档、报表、图片、数据导出、打包结果等），"
        "需要交付给用户时调用本工具；入参为工作区内文件的本地路径。"
    ),
    input_params={
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "待上传文件在当前工作区内的本地路径（绝对或相对工作区）",
            },
        },
        "required": ["file_path"],
    },
)


def _get_mime_type(file_name: str) -> str:
    """根据文件扩展名获取MIME类型。"""
    ext = os.path.splitext(file_name)[1].lower()
    mime_types = {
        ".pdf": "application/pdf",
        ".doc": "application/msword",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".xls": "application/vnd.ms-excel",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".ppt": "application/vnd.ms-powerpoint",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".txt": "text/plain",
        ".csv": "text/csv",
        ".json": "application/json",
        ".xml": "application/xml",
        ".html": "text/html",
        ".htm": "text/html",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
        ".webp": "image/webp",
        ".svg": "image/svg+xml",
        ".mp3": "audio/mpeg",
        ".mp4": "video/mp4",
        ".wav": "audio/wav",
        ".zip": "application/zip",
        ".rar": "application/x-rar-compressed",
        ".tar": "application/x-tar",
        ".gz": "application/gzip",
        ".7z": "application/x-7z-compressed",
        ".py": "text/x-python",
        ".js": "application/javascript",
        ".ts": "application/typescript",
        ".java": "text/x-java-source",
        ".c": "text/x-c",
        ".cpp": "text/x-c++",
        ".go": "text/x-go",
        ".rs": "text/x-rust",
        ".md": "text/markdown",
    }
    return mime_types.get(ext, "application/octet-stream")


async def _upload_file_tool_impl(**inputs: Any) -> dict[str, Any]:
    """Upload a file to OBS and return the file info with CDN URL."""
    file_path = inputs.get("file_path", "")
    logger.info("[upload_file_tool] 开始执行, file_path=%s", file_path)

    if not file_path:
        logger.error("[upload_file_tool] file_path 为空")
        return UploadFileErrorOutput(error="file_path 为必填参数").to_dict()

    # 路径安全校验（工作区边界 + 存在性 + 类型）
    is_valid, resolved_path, error_msg = resolve_and_validate_path(
        file_path,
        must_exist=True,
        must_be_file=True,
    )
    if not is_valid:
        logger.error("[upload_file_tool] 路径校验失败: %s", error_msg)
        return UploadFileErrorOutput(error=error_msg or "路径校验失败").to_dict()

    file_path = resolved_path
    logger.debug("[upload_file_tool] 标准化后路径: %s", file_path)

    # 获取文件信息
    file_name = os.path.basename(file_path)
    file_size = os.path.getsize(file_path)
    mime_type = _get_mime_type(file_name)
    logger.info(
        "[upload_file_tool] 文件校验通过, name=%s, size=%d bytes, mime=%s",
        file_name, file_size, mime_type
    )

    try:
        logger.info("[upload_file_tool] 开始上传文件到 OBS: %s", file_path)
        uploader = create_upload_file_obs()
        url = await uploader.upload_file(file_path)
        if not url:
            logger.error("[upload_file_tool] 上传返回空 URL: %s", file_path)
            return UploadFileErrorOutput(error="上传异常，未获取到 OBS CDN 链接").to_dict()

        logger.info("[upload_file_tool] 上传成功, url=%s", url)
        return UploadFileSuccessOutput(
            url=url,
            name=file_name,
            size_bytes=str(file_size),
            mime=mime_type,
        ).to_dict()

    except Exception as e:
        # 详细错误记录到日志，向用户返回泛化错误
        logger.exception("[upload_file_tool] 上传过程中发生错误: %s", e)
        return UploadFileErrorOutput(error="文件上传失败，请稍后重试").to_dict()


def get_upload_file_tool() -> LocalFunction:
    return LocalFunction(card=_UPLOAD_FILE_TOOL_CARD, func=_upload_file_tool_impl)
