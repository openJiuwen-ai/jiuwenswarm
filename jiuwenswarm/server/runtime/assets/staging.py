# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""资产导入导出的暂存目录、TTL 回收与公共暂存落位。

导出产物与导入上传都落在用户工作区下的受控目录，随实例数据目录隔离：

- ``<workspace>/temp/asset_exports`` —— assets.export 产物，下载 token 指向；
- ``<workspace>/temp/asset_uploads`` —— /file-api/assets/upload-temp 上传
  暂存，每次上传一个独立子目录；返回给前端的 path 仅在此根内有效，
  导入入口据此拒绝任意磁盘路径。

TTL 回收（默认 1 小时）是兜底：产物本应由调用方用完即删，但浏览器
崩溃、页面中途关闭会留下孤儿文件，上传/导出入口顺带清理。

暂存落位由 :func:`store_staged_package` 单点维护：upload-temp（HTTP
multipart 字节）与 assets.stage_local（本机路径复制）两个入口只有来源
取法不同，文件名合法性、.zip 格式、体积上限、TTL 顺带回收与独立子
目录布局在此共享同一实现，防护调整只改一处。
"""

from __future__ import annotations

import shutil
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from jiuwenswarm.common.safe_archive import ArchiveLimits
from jiuwenswarm.server.runtime.assets.contract import is_illegal_filename
from jiuwenswarm.server.runtime.assets.errors import AssetError, AssetErrorCode

STAGING_TTL_SECONDS = 3600

EXPORT_ROOT_NAME = "asset_exports"
UPLOAD_ROOT_NAME = "asset_uploads"


def _staging_base() -> Path:
    # 延迟导入：单测以 monkeypatch 替换 get_workspace_dir 隔离临时目录。
    from jiuwenswarm.common.utils import get_workspace_dir

    return get_workspace_dir() / "temp"


def assets_export_root() -> Path:
    """导出产物根目录（不存在则创建）。"""
    root = _staging_base() / EXPORT_ROOT_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def assets_upload_root() -> Path:
    """导入上传暂存根目录（不存在则创建）。"""
    root = _staging_base() / UPLOAD_ROOT_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def store_staged_package(
    *,
    filename: Any,
    size_bytes: int,
    write: Callable[[Path], None],
    limits: ArchiveLimits = ArchiveLimits(),
) -> Path:
    """校验并把一个资产包写入受控上传暂存根，返回落位路径。

    防护与布局（两上传入口共享的契约）：文件名合法性（``is_illegal_filename``
    → ASSET_UNSAFE_PATH）、仅 .zip 与体积上限（→ ASSET_INVALID_PACKAGE /
    ASSET_LIMIT_EXCEEDED）、写入前对上传根做 TTL 顺带回收、每次上传落
    独立 uuid 子目录（同名不冲突）。

    ``write`` 回调对给定目标路径执行实际写入（multipart 字节或本机文件
    复制）；写入抛出的任何异常在回收本次子目录后原样上抛，错误收敛留给
    各入口层（HTTP 层映射状态码，RPC 层走 converge_error_payload）。
    """
    if is_illegal_filename(filename):
        raise AssetError(AssetErrorCode.UNSAFE_PATH, "文件名非法")
    if not str(filename).lower().endswith(".zip"):
        raise AssetError(AssetErrorCode.INVALID_PACKAGE, "资产包仅支持 .zip 格式")
    if size_bytes > limits.zip_bytes:
        raise AssetError(AssetErrorCode.LIMIT_EXCEEDED, "压缩包体积超限")

    upload_root = assets_upload_root()
    prune_stale_entries(upload_root)
    upload_dir = upload_root / uuid.uuid4().hex
    upload_dir.mkdir(parents=True)
    dest = upload_dir / str(filename)
    try:
        write(dest)
    except BaseException:
        shutil.rmtree(upload_dir, ignore_errors=True)
        raise
    return dest


def prune_stale_entries(
    root: Path,
    *,
    max_age_s: int = STAGING_TTL_SECONDS,
    now: float | None = None,
) -> int:
    """删除 root 下超过 TTL 的直接子条目，返回删除个数。

    年龄按条目自身 mtime（lstat，不跟随 symlink）判定；删除尽力而为——
    单个条目失败（被占用、权限）时跳过其余继续，回收失败不得阻断
    上传/导出主流程。symlink（含指向目录的）只删链接本身，不跟随、
    不触碰链接目标。
    """
    root = Path(root)
    if not root.is_dir():
        return 0
    cutoff = (now if now is not None else time.time()) - max_age_s
    removed = 0
    for child in root.iterdir():
        try:
            if child.lstat().st_mtime > cutoff:
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
            removed += 1
        except OSError:
            continue
    return removed
