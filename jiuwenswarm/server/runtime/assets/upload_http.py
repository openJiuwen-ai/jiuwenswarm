# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``/file-api/assets/upload-temp`` HTTP 面：multipart 上传 → 本地暂存路径。

前端先把资产包 POST 到这里拿到 ``{path}``，再调 ``assets.import
{path, kind?}`` 完成导入。与技能上传（/file-api/skills/upload-temp）
同构，但按统一资产契约收敛错误码：

- 只接受 ``.zip``（统一资产包格式），非法文件名按 ASSET_UNSAFE_PATH 拒绝；
- 可选 ``kind`` 字段在上传期即做契约层归一化校验，未知 kind 快速失败；
- 每次上传落独立子目录（同名不冲突），路径仅位于受控上传根内；
- 上传前顺带对上传根做 TTL 回收（兜底清理孤儿文件）；
- 包体大小按安全归档层 zip 上限复核（HTTP 层已按 Content-Length 预检）。
"""

from __future__ import annotations

import logging
import shutil
import uuid
from pathlib import PureWindowsPath
from typing import Any

from jiuwenswarm.common.multipart_form import MultipartParseError, parse_multipart_form
from jiuwenswarm.common.safe_archive import ArchiveLimits
from jiuwenswarm.server.runtime.assets.contract import normalize_asset_kind
from jiuwenswarm.server.runtime.assets.errors import (
    AssetErrorCode,
    asset_http_error_body,
    asset_http_error_status,
)
from jiuwenswarm.server.runtime.assets.staging import (
    assets_upload_root,
    prune_stale_entries,
)

logger = logging.getLogger(__name__)

# 请求体上限：zip 包上限 50MB + 2MB multipart 封装余量。HTTP 层按
# Content-Length 预检，避免超大 body 整读进内存；handler 再按实际字节复核。
MAX_ASSETS_UPLOAD_BYTES = ArchiveLimits().zip_bytes + 2 * 1024 * 1024


def _reject(code: str, message: str) -> tuple[int, dict[str, Any]]:
    return asset_http_error_status(code), asset_http_error_body(code, message)


def _is_illegal_filename(filename: Any) -> bool:
    """上传文件名合法性：须为非空常规名，不含路径分隔符或 NUL，非 Windows 保留名。"""
    if not isinstance(filename, str) or filename in {"", ".", ".."}:
        return True
    if any(char in filename for char in "/\\:\x00"):
        return True
    return PureWindowsPath(filename).is_reserved()


def handle_assets_upload_temp_http(
    *,
    content_type: str,
    body: bytes,
    limits: ArchiveLimits = ArchiveLimits(),
) -> tuple[int, dict[str, Any]]:
    """保存上传的资产包，返回 ``200 {"path": ...}`` 供 assets.import 使用。"""
    try:
        try:
            fields = parse_multipart_form(content_type, body)
        except MultipartParseError as exc:
            return _reject(AssetErrorCode.INVALID_PACKAGE, str(exc))

        kind_raw = fields.get("kind")
        if kind_raw is not None and str(kind_raw).strip():
            try:
                normalize_asset_kind(kind_raw)
            except ValueError as exc:
                return _reject(AssetErrorCode.INVALID_PACKAGE, str(exc))

        file_field = fields.get("file")
        if not isinstance(file_field, dict) or not isinstance(
            file_field.get("content"), (bytes, bytearray)
        ):
            return _reject(AssetErrorCode.INVALID_PACKAGE, "缺少 file 字段")
        filename = file_field.get("filename")
        if _is_illegal_filename(filename):
            return _reject(AssetErrorCode.UNSAFE_PATH, "文件名非法")
        if not filename.lower().endswith(".zip"):
            return _reject(AssetErrorCode.INVALID_PACKAGE, "资产包仅支持 .zip 格式")
        content = bytes(file_field["content"])
        if len(content) > limits.zip_bytes:
            return _reject(AssetErrorCode.LIMIT_EXCEEDED, "压缩包体积超限")

        upload_root = assets_upload_root()
        prune_stale_entries(upload_root)
        upload_dir = upload_root / uuid.uuid4().hex
        upload_dir.mkdir(parents=True)
        dest = upload_dir / filename
        try:
            dest.write_bytes(content)
        except OSError:
            shutil.rmtree(upload_dir, ignore_errors=True)
            raise
        return 200, {"path": str(dest)}
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        logger.exception("[assets_upload_http] temporary upload failed: %s", exc)
        return 500, asset_http_error_body("INTERNAL_ERROR", str(exc))
