# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""assets.export / assets.import 业务入口（kind 无关编排层）。

- 导出：适配器定位物料 → 单根打包到受控导出目录 → 签发短效下载 token →
  返回 ``{download_url, filename, size_bytes, sha256}``；
- 导入：path 必须来自 ``/file-api/assets/upload-temp`` 的受控暂存 →
  安全解包 → 缺省 kind 时按适配器注册顺序嗅探 → 分发导入 → 返回
  ``{kind, id, pending_connectors?}``。

本层向上抛出的异常由入口 handler 收敛：``AssetError`` 透传、带 ``.code``
的内部异常（SafeArchiveError 等）查表映射、无内部码的 ``ValueError``
视为参数非法、其余 INTERNAL_ERROR 兜底（见 errors 模块约定）。
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

from jiuwenswarm.common.file_download_token import (
    PURPOSE_ASSET_EXPORT,
    build_file_download_info,
)
from jiuwenswarm.common.safe_archive import (
    extract_archive_safely,
    pack_directory_single_root,
)
from jiuwenswarm.server.runtime.assets.contract import (
    AssetKind,
    build_export_filename,
    parse_export_params,
    parse_import_params,
    validate_asset_id,
)
from jiuwenswarm.server.runtime.assets.errors import AssetError, AssetErrorCode
from jiuwenswarm.server.runtime.assets.registry import (
    get_asset_adapter,
    registered_asset_kinds,
)
from jiuwenswarm.server.runtime.assets.staging import (
    assets_export_root,
    assets_upload_root,
    prune_stale_entries,
)

logger = logging.getLogger(__name__)


def _install_default_adapters() -> None:
    """装配默认真实适配器（skill 等）。

    延迟且幂等：HTTP（handlers/assets）与 WS（agent_adapter）两个入口
    共用本模块，首次调用前完成注册；未注册的 kind 收敛为契约错误。
    """
    from jiuwenswarm.server.runtime.assets.adapters import install_default_adapters

    install_default_adapters()


_install_default_adapters()

# 导出下载 token 有效期（秒）：导出后应立即完成下载，短时效缩小
# token 泄露后的可用窗口。
EXPORT_DOWNLOAD_EXPIRES_S = 600


def export_asset(
    params: Any,
    *,
    session_id: str = "",
    user_id: str = "",
) -> dict[str, Any]:
    """执行 assets.export，返回 ``{download_url, filename, size_bytes, sha256}``。"""
    parsed = parse_export_params(params)
    kind: AssetKind = parsed["kind"]
    version = parsed.get("version")
    adapter = get_asset_adapter(kind)
    if adapter is None:
        # kind 合法但本部署未实现导出（如预留的 plugin）：按受限资产不可导出。
        raise AssetError(
            AssetErrorCode.EXPORT_FORBIDDEN,
            f"资产类型 {kind.value} 暂不支持导出",
            details={"kind": kind.value},
        )
    source = adapter.locate_export(parsed["local_id"], version)
    asset_id = validate_asset_id(source.asset_id)

    export_root = assets_export_root()
    prune_stale_entries(export_root)
    filename = build_export_filename(asset_id, source.version or version)
    # 每次导出落独立子目录：并发导出同一资产不互相覆盖；子目录名随机，
    # 包文件 basename 保持契约文件名（下载端按 basename 生成
    # Content-Disposition）。
    export_dir = export_root / uuid.uuid4().hex[:12]
    export_dir.mkdir(parents=True)
    zip_path = export_dir / filename
    try:
        packed = pack_directory_single_root(
            source.source_dir,
            zip_path,
            root_name=asset_id,
            exclude_paths=source.exclude_paths,
        )
        info = build_file_download_info(
            str(zip_path),
            filename,
            session_id=session_id,
            expires_in=EXPORT_DOWNLOAD_EXPIRES_S,
            user_id=user_id,
            purpose=PURPOSE_ASSET_EXPORT,
        )
    except BaseException:
        shutil.rmtree(export_dir, ignore_errors=True)
        raise
    return {
        "download_url": info["download_url"],
        "filename": filename,
        "size_bytes": packed.artifact.size_bytes,
        "sha256": packed.artifact.sha256,
    }


def import_asset(params: Any) -> dict[str, Any]:
    """执行 assets.import，返回 ``{kind, id, pending_connectors?}``。"""
    parsed = parse_import_params(params)
    staged_path = _resolve_upload_path(Path(parsed["path"]))
    kind_hint: AssetKind | None = parsed.get("kind")

    work_dir = Path(tempfile.mkdtemp(prefix="jiuwenswarm_asset_import_"))
    try:
        extracted = extract_archive_safely(staged_path, work_dir / "unpacked")
        if extracted.root_name is None:
            raise AssetError(
                AssetErrorCode.INVALID_PACKAGE,
                "资产包必须为单层根目录结构（<id>/…）",
            )
        package_dir = work_dir / "unpacked" / extracted.root_name
        kind = kind_hint if kind_hint is not None else _sniff_kind(package_dir)
        adapter = get_asset_adapter(kind)
        if adapter is None:
            # kind 显式给定但未注册（如预留的 plugin）：包本身合法，
            # 按无效资产包拒绝并提示类型不支持。
            raise AssetError(
                AssetErrorCode.INVALID_PACKAGE,
                f"资产类型 {kind.value} 暂不支持导入",
                details={"kind": kind.value},
            )
        outcome = adapter.import_package(package_dir)
        payload: dict[str, Any] = {
            "kind": kind.value,
            "id": validate_asset_id(outcome.asset_id),
        }
        if outcome.pending_connectors:
            payload["pending_connectors"] = list(outcome.pending_connectors)
        return payload
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _resolve_upload_path(raw_path: Path) -> Path:
    """校验导入 path 来自受控上传暂存：非 symlink、根内、存在的 .zip。"""
    if raw_path.is_symlink():
        raise AssetError(AssetErrorCode.UNSAFE_PATH, "导入路径不得为符号链接")
    resolved = raw_path.resolve()
    upload_root = assets_upload_root().resolve()
    if not resolved.is_relative_to(upload_root):
        raise AssetError(
            AssetErrorCode.UNSAFE_PATH,
            "path 必须来自 /file-api/assets/upload-temp 返回的暂存路径",
        )
    if not resolved.is_file() or resolved.suffix.lower() != ".zip":
        raise AssetError(
            AssetErrorCode.INVALID_PACKAGE,
            "上传文件不存在或已过期，请重新上传",
        )
    return resolved


def _sniff_kind(package_dir: Path) -> AssetKind:
    """缺省 kind 时按适配器注册顺序嗅探；均不匹配按无效包拒绝。"""
    for kind in registered_asset_kinds():
        adapter = get_asset_adapter(kind)
        if adapter is None:
            continue
        try:
            if adapter.sniff_package(package_dir):
                return kind
        except Exception:  # noqa: BLE001 单个适配器嗅探失败不阻断其余 kind
            logger.warning(
                "[assets] sniff_package failed for kind=%s", kind.value, exc_info=True
            )
    raise AssetError(
        AssetErrorCode.INVALID_PACKAGE,
        "无法识别资产类型：包内缺少类型标记文件，请在导入时显式指定 kind",
    )
