# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Bounded, deterministic publishing snapshots; never mutate installed assets.

安全打包原语（安全收集、防 TOCTOU 复制摘要、确定性 zip 写入、环境模板
凭证扫描）位于 :mod:`jiuwenswarm.common.safe_archive`，发布 / 导出 / 导入
三场景共用。本模块只保留发布布局薄层：快照归一化校验、源目录一致性复核、
发布包布局组装（skill / skillpack 双层目录 + plugin.yaml 外壳）。
旧名 ``PackageBuildError`` / ``PackageLimits`` / ``collect_publish_files``
以别名 re-export，既有调用方行为不变。
"""

from dataclasses import dataclass
from pathlib import Path
import shutil
import tempfile
from typing import cast

import zipfile

import yaml

from jiuwenswarm.common.safe_archive import (
    ArchiveLimits,
    SafeArchiveError,
    _is_link,
    collect_archive_files,
    copy_files_digest,
    validate_archive_root_name,
    validate_environment_template,
    write_zip_entries,
)

from .asset_publish_models import APP_UPLOAD_BODY_BYTES, PublishIdentity

# 向后兼容别名：异常与限额同安全归档公共层，捕获 PackageBuildError 的
# 既有代码语义不变。SafeArchiveError 继承 ValueError，因此仓库内既有的
# 宽泛 ``except ValueError`` 收敛（skillpack_publish_service 等）行为一致。
PackageBuildError = SafeArchiveError
PackageLimits = ArchiveLimits
collect_publish_files = collect_archive_files

# app 载荷限额：Hub 对 app 包的压缩前体积上限更宽，但条目数收紧。
_APP_LIMITS = PackageLimits(zip_bytes=APP_UPLOAD_BODY_BYTES, max_files=1000)


@dataclass(frozen=True)
class PreparedPackage:
    """发布产物与打包过程的元数据快照（供发布服务与审计使用）。"""

    artifact_path: Path
    artifact_sha256: str
    size_bytes: int
    source_digest: str
    files: tuple[str, ...]
    excluded: tuple[str, ...]
    normalizations: tuple[str, ...]
    dependencies: tuple[dict, ...]


def build_asset_package(
    source: Path,
    identity: PublishIdentity,
    metadata: dict,
    output_dir: Path,
    *,
    limits: PackageLimits | None = None,
    exclude_paths: tuple[Path, ...] = (),
) -> PreparedPackage:
    """发布场景打包：安全快照 → 归一化 → 布局组装 → 确定性 zip。"""
    from . import asset_publish_adapters

    app_payload = identity.kind == "app"
    if limits is None:
        limits = _APP_LIMITS if app_payload else PackageLimits()
    source, output_dir = Path(source), Path(output_dir)
    if not source.is_dir() or _is_link(source):
        raise PackageBuildError("UNSAFE_PATH")
    # Resolve stable OS ancestors (e.g. macOS /var -> /private/var) once.
    # The subsequent descriptor walk rejects any new symlink at that canonical path.
    source = source.resolve()
    exclude_paths = tuple(Path(path).resolve() for path in exclude_paths)
    if output_dir.resolve().is_relative_to(source.resolve()):
        raise PackageBuildError("OUTPUT_INSIDE_SOURCE")
    name = validate_archive_root_name(identity.package_name)
    files, excluded = collect_publish_files(
        source, limits=limits, exclude_paths=exclude_paths, app_payload=app_payload
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="publish-", dir=output_dir))
    snapshot = stage / "snapshot" / name
    snapshot.mkdir(parents=True)
    try:
        source_digest = copy_files_digest(source, files, limits, snapshot)
        for template in snapshot.rglob("*"):
            filename = template.name.casefold() if app_payload else template.name
            if (
                filename in {".env.example", ".env.template"}
                and template.is_file()
            ):
                validate_environment_template(template)
        details = asset_publish_adapters.normalize_and_validate(
            snapshot, identity, metadata
        )
        current_files, _ = collect_publish_files(
            source, limits=limits, exclude_paths=exclude_paths, app_payload=app_payload
        )
        if source_digest != copy_files_digest(source, current_files, limits):
            raise PackageBuildError("SOURCE_CHANGED")
        normalized, _ = collect_publish_files(
            snapshot, limits=limits, app_payload=app_payload
        )
        entries = {}
        for path in normalized:
            rel = path.relative_to(snapshot).as_posix()
            wrapped = identity.kind in {"skill", "skillpack", "app"}
            entries[f"{name}/{name}/{rel}" if wrapped else rel] = path
        wrapper = details.get("wrapper")
        if wrapper:
            entries[f"{name}/plugin.yaml"] = yaml.safe_dump(
                wrapper, sort_keys=True, allow_unicode=True
            ).encode()
        icon_png = details.get("icon_png")
        if isinstance(icon_png, (bytes, bytearray)) and icon_png:
            entries[f"{name}/icon.png"] = bytes(icon_png)
        if identity.kind == "skill" and (snapshot / "README.md").is_file():
            entries[f"{name}/README.md"] = snapshot / "README.md"
        artifact_path = stage / "artifact.zip"
        # Stored app entries stay within Hub's compression ratio limit.
        compression = zipfile.ZIP_STORED if app_payload else zipfile.ZIP_DEFLATED
        artifact = write_zip_entries(
            entries, artifact_path, limits=limits, compression=compression
        )
        return PreparedPackage(
            artifact_path,
            artifact.sha256,
            artifact.size_bytes,
            source_digest,
            tuple(sorted(entries)),
            tuple(excluded),
            tuple(cast("list[str]", details.get("normalizations", []))),
            tuple(cast("list[dict]", details.get("dependencies", []))),
        )
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(stage / "snapshot", ignore_errors=True)
