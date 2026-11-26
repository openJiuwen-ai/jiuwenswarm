# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""assets.export / assets.import 业务入口（kind 无关编排层）。

- 导出：适配器定位物料 → 单根打包到受控导出目录 → 签发短效下载 token →
  返回 ``{download_url, filename, size_bytes, sha256}``；
- 导入：path 必须来自 ``/file-api/assets/upload-temp`` 的受控暂存 →
  安全解包 → 缺省 kind 时按适配器注册顺序嗅探 → 分发导入 → 返回
  ``{kind, id, pending_connectors?}``；
- 桌面暂存：``assets.stage_local`` 把桌面壳给出的本机绝对路径复制进
  与 upload-temp 相同的受控暂存根（同防护），返回 ``{path}``，前端
  两通道统一走 ``assets.import``；仅 WS 入口，有意不注册 HTTP 北向
  （见 ``stage_local_asset`` docstring）。

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
    ArchiveLimits,
    extract_archive_safely,
    pack_directory_single_root,
)
from jiuwenswarm.server.runtime.assets.contract import (
    AssetKind,
    build_export_filename,
    is_windows_reserved_stem,
    parse_export_params,
    parse_import_params,
    parse_stage_local_params,
    sanitize_export_stem,
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
    store_staged_package,
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

# 下载文件名显示名主干的上限（UTF-8 字节）。文件名单段受文件系统
# NAME_MAX（常见 255 字节）约束，而显示名是用户自由输入、可能很长；
# 不截断会让 zip 落盘时才抛 OSError（非 AssetError，被兜底成
# INTERNAL_ERROR）。取 120 字节为版本后缀与 ".zip" 留足余量。只截显示
# 名，回退的 asset_id 不截（其长度由包命名约束，且 skill 导出走该回退，
# 行为须逐字节不变）。
_MAX_EXPORT_STEM_BYTES = 120


def _truncate_export_stem(stem: str, budget: int = _MAX_EXPORT_STEM_BYTES) -> str:
    """把已清洗的文件名主干按字节预算截断，不切断多字节字符。"""
    encoded = stem.encode("utf-8")
    if len(encoded) <= budget:
        return stem
    return encoded[:budget].decode("utf-8", "ignore")


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
    # 下载文件名主干优先取 UI 显示名（与界面列表一致），缺省回退 asset_id；
    # 显示名先经 sanitize 清洗、再按字节预算截断，产物不含路径分隔符与文件名
    # 特殊字符、不超文件名单段长度上限，必然通过 build_export_filename 内部的
    # 安全校验。显示名命中 Windows 保留设备名（NUL、CON.txt 等）时视同不可
    # 用：否则 Windows 部署到落盘才抛 OSError（非 AssetError，兜底成
    # INTERNAL_ERROR）；asset_id 是既有包目录名，在当前文件系统必然可写。
    # 包内顶层目录仍为 asset_id（root_name 单独传入），不受下载文件名影响。
    display_stem = _truncate_export_stem(sanitize_export_stem(source.display_name))
    filename_stem = (
        asset_id
        if not display_stem or is_windows_reserved_stem(display_stem)
        else display_stem
    )
    filename = build_export_filename(filename_stem, source.version or version)
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
            extra_files=source.extra_files,
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


def stage_local_asset(params: Any, *, limits: ArchiveLimits = ArchiveLimits()) -> dict[str, Any]:
    """执行 assets.stage_local：把本机绝对路径的资产包复制进受控上传暂存根。

    桌面壳原生选择器/拖拽只给出本机路径，不经 HTTP multipart；本入口只做
    来源侧校验（本机真实文件、非符号链接、绝对路径），文件名/格式/体积
    防护与暂存落位走 ``staging.store_staged_package``——与 upload-temp
    同一实现、同一暂存根，返回 ``{path}`` 供前端统一调 ``assets.import``。

    接受任意本机绝对路径是有意设计，不视为越权面扩大：本 RPC 与被它替代
    的旧 ``agent_templates.import_local`` 同属本机管理面信任模型（详情页
    文件读取等既有 RPC 同样按客户端给出的路径读服务端文件），未引入既有
    RPC 面之外的新能力；且此处只收紧不放松——仅 .zip、拒符号链接、限额、
    复制入受控暂存根后 ``assets.import`` 只读暂存根，不再触碰原路径。
    暴露面在构造上收敛为 WS-only：本方法有意不注册 dispatch.HANDLERS、
    pod_rest_map 不声明 REST 路由，HTTP 北向不可达（旧
    ``agent_templates.import_local`` 同为 WS-only，暴露面持平）。
    远程部署若需收口「任意路径」类入参，应作为覆盖整个管理面 RPC 的
    统一加固任务（来源门控/白名单），不在本入口单点处理。
    """
    parsed = parse_stage_local_params(params)
    source = Path(parsed["path"])
    if source.is_symlink():
        raise AssetError(AssetErrorCode.UNSAFE_PATH, "导入路径不得为符号链接")
    if not source.is_file():
        raise AssetError(AssetErrorCode.INVALID_PACKAGE, "本地文件不存在或不可读")
    try:
        size = source.stat().st_size
    except OSError:
        raise AssetError(AssetErrorCode.INVALID_PACKAGE, "本地文件不存在或不可读") from None

    def _copy(dest: Path) -> None:
        try:
            shutil.copyfile(source, dest)
        except OSError:
            raise AssetError(AssetErrorCode.INVALID_PACKAGE, "本地文件读取失败") from None

    dest = store_staged_package(
        filename=source.name, size_bytes=size, write=_copy, limits=limits
    )
    return {"path": dest.as_posix()}


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
