# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2025. All rights reserved.
import asyncio
import os
import uuid
from pathlib import Path
import requests
import logging
from urllib.parse import urlparse

from .file import get_task_dir

logger = logging.getLogger(__name__)

ALLOWED_EXTS = {
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".txt", ".xls", ".xlsx",
    ".md", ".jpg", ".jpeg", ".png", ".zip", ".skill", ".py"
}


def add_random_suffix_uuid(filename):
    """
    使用UUID为文件名添加随机后缀
    """
    name, ext = os.path.splitext(filename)
    random_suffix = str(uuid.uuid4())[:8]  # 取前8位
    return f"{name}_{random_suffix}{ext}"


def check_path_has_cross_dir(dir_or_file_name: str) -> bool:
    patterns = ["../", "/..", "..\\", "\\..", "./", ".\\.\\", "%00"]
    return any(p in dir_or_file_name for p in patterns)


def check_save_path(save_path: str) -> bool:
    if check_path_has_cross_dir(str(save_path)):
        logger.error("下载失败: 文件名非法")
        return False

    ext = Path(save_path).suffix.lower()
    if ext not in ALLOWED_EXTS:
        logger.error(f"下载失败: 不支持的文件类型 {ext}")
        return False

    return True


def check_save_dir_and_no_overwrite(save_path: str) -> bool:
    path = Path(save_path)

    # 目录是否存在
    if not path.parent.is_dir():
        logger.error("下载失败: 保存目录不存在")
        return False

    return True


async def download_file(url, save_path):
    """
    下载文件并保存到本地
    url: 文件下载链接
    save_path: 本地保存路径
    """

    # 安全校验：文件类型和文件最大尺寸
    MAX_SIZE_BYTES = 150 * 1024 * 1024

    try:
        # 安全校验：文件名/路径跨目录片段
        if not check_save_path(save_path):
            raise RuntimeError("download failed: invalid filename or unsupported file type")

        if not check_save_dir_and_no_overwrite(save_path):
            raise RuntimeError("download failed: save directory does not exist or file already exists")

        new_url = await non_edge_convert_edge_osms_link(url)

        response = requests.get(new_url, stream=True, timeout=10)
        response.raise_for_status()

        content_length = response.headers.get("Content-Length")
        if content_length and content_length.isdigit() and int(content_length) > MAX_SIZE_BYTES:
            logger.error("下载失败: 文件大小超过 150MB")
            raise RuntimeError("download failed: file size exceeds 150MB")

        total = 0
        with open(save_path, 'wb') as file:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    total += len(chunk)
                    if total > MAX_SIZE_BYTES:
                        logger.error("下载失败: 文件大小超过 150MB")
                        raise RuntimeError("download failed: file size exceeds 150MB")
                    file.write(chunk)

        logger.info(f"下载成功！文件已保存至当前目录下的: {save_path}")
        return save_path

    except requests.exceptions.RequestException as e:
        logger.error(f"下载失败: {type(e).__name__} ")
        raise RuntimeError(f"download failed, type: {type(e).__name__}", e) from e
    except Exception as e:
        logger.error(f"发生错误: {type(e).__name__} ")
        raise RuntimeError(f"download failed, type: {type(e).__name__}", e) from e


async def download_multiple_files(urls_and_paths):
    """
    异步下载多个文件
    urls_and_paths: [(url1, path1), (url2, path2), ...]
    """
    tasks = []
    for url, file_name in urls_and_paths:
        logger.info(f"开始下载,保存文件名：{file_name}")
        save_path = os.path.join(get_task_dir(), file_name)
        task = download_file(url, save_path)
        tasks.append(task)

    results = await asyncio.gather(*tasks)
    return results


async def non_edge_convert_edge_osms_link(url):
    """osms-obs链接：在贵阳上将 大搜的Non-edge 链接转换为 edge 链接"""

    parse_url = urlparse(url)
    if "/osms/" not in parse_url.path:
        logger.info("非osms链接，无须替换")
        return url

    if not os.environ['OBS_TRANSFER_USE_EDGE'].lower() == 'true':
        logger.info("非贵阳环境，无须替换")
        return url

    return url
