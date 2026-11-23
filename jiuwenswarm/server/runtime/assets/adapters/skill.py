# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""skill 资产的导出/导入适配器。

- 导出：本机已安装的 skill（自建、市场安装）与系统内置（built-in）均
  允许导出（设计方案 2.5 权限规则）；导出包为 ``<id>/SKILL.md`` 及
  附属目录的单根结构（2.2），文件名 ``<id>[-<version>].zip``，version
  取自 SKILL.md frontmatter 的 ``version`` 字段（缺省不带）。
- 导入：校验 SKILL.md frontmatter（name/description 非空）与 ``<id>``
  一致性；同名本地 skill 拒绝覆盖（ALREADY_EXISTS），与内置同名视为
  只读冲突（BUILTIN_READ_ONLY），内容复制进本地 skills 目录后即返回，
  不保留临时路径引用。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from jiuwenswarm.server.runtime.assets.contract import AssetKind
from jiuwenswarm.server.runtime.assets.errors import AssetError, AssetErrorCode
from jiuwenswarm.server.runtime.assets.registry import ExportSource, ImportOutcome

# 内置技能目录型目录（skill_manager._BUILTIN_CATALOG_NAME 同源约定）。
_BUILTIN_CATALOG_DIR = "workswarm-builtin"


def _skills_dir() -> Path:
    # 延迟导入：单测以 monkeypatch 替换 utils 的目录函数隔离临时目录。
    from jiuwenswarm.common.utils import get_agent_skills_dir

    return get_agent_skills_dir()


def _builtin_skills_dir() -> Path:
    from jiuwenswarm.common.utils import get_builtin_skills_dir

    return get_builtin_skills_dir()


def _read_frontmatter(skill_md: Path) -> dict[str, Any]:
    """复用 skill_manager 的公开解析入口（YAML 语义与导入校验一致）。"""
    from jiuwenswarm.server.runtime.skill.skill_manager import parse_skill_frontmatter

    metadata, _ = parse_skill_frontmatter(skill_md.read_text(encoding="utf-8"))
    return metadata


def _skill_dir_candidates(local_id: str) -> tuple[Path, ...]:
    """local_id 的候选安装位置：本地 skills 目录与内置目录（扁平/目录型）。"""
    builtin = _builtin_skills_dir()
    return (
        _skills_dir() / local_id,
        builtin / local_id,
        builtin / _BUILTIN_CATALOG_DIR / local_id,
    )


class SkillAssetAdapter:
    """skill 资产适配器：定位、嗅探与本地 skills 目录导入。"""

    kind = AssetKind.SKILL

    def locate_export(self, local_id: str, version: str | None) -> ExportSource:
        """定位本机 skill 目录（本地优先、内置兜底）；版本取自 frontmatter。"""
        source_dir = next(
            (
                candidate
                for candidate in _skill_dir_candidates(local_id)
                if (candidate / "SKILL.md").is_file()
            ),
            None,
        )
        if source_dir is None:
            raise AssetError(
                AssetErrorCode.NOT_FOUND,
                f"skill {local_id!r} 未安装到本机",
                details={"id": local_id},
            )
        metadata = self._read_validated_frontmatter(source_dir / "SKILL.md", local_id)
        actual_version = metadata.get("version")
        actual_version = (
            actual_version.strip()
            if isinstance(actual_version, str) and actual_version.strip()
            else None
        )
        if version is not None and version != actual_version:
            raise AssetError(
                AssetErrorCode.NOT_FOUND,
                f"skill {local_id!r} 不存在版本 {version!r}",
                details={"id": local_id, "version": version},
            )
        return ExportSource(
            source_dir=source_dir, asset_id=local_id, version=actual_version
        )

    @staticmethod
    def sniff_package(package_dir: Path) -> bool:
        """包顶层含 SKILL.md 即视为 skill 包。"""
        return (Path(package_dir) / "SKILL.md").is_file()

    def import_package(self, package_dir: Path) -> ImportOutcome:
        """校验并复制进本地 skills 目录；同名冲突与内置只读拒绝覆盖。"""
        package_dir = Path(package_dir)
        asset_id = package_dir.name
        metadata = self._read_validated_frontmatter(package_dir / "SKILL.md", asset_id)
        name = metadata.get("name")
        if not isinstance(name, str) or not name.strip():
            raise AssetError(
                AssetErrorCode.INVALID_PACKAGE,
                "SKILL.md frontmatter 须含非空 name",
                details={"id": asset_id},
            )
        if name.strip() != asset_id:
            raise AssetError(
                AssetErrorCode.INVALID_PACKAGE,
                f"SKILL.md name {name.strip()!r} 与包顶层目录 {asset_id!r} 不一致",
                details={"id": asset_id, "name": name.strip()},
            )
        target = _skills_dir() / asset_id
        if (target / "SKILL.md").is_file():
            raise AssetError(
                AssetErrorCode.ALREADY_EXISTS,
                f"本地已存在同名 skill {asset_id!r}，禁止覆盖",
                details={"id": asset_id},
            )
        builtin_conflict = next(
            (
                candidate
                for candidate in _skill_dir_candidates(asset_id)
                if (candidate / "SKILL.md").is_file()
            ),
            None,
        )
        if builtin_conflict is not None:
            raise AssetError(
                AssetErrorCode.BUILTIN_READ_ONLY,
                f"skill {asset_id!r} 与系统内置资产同名，禁止覆盖",
                details={"id": asset_id},
            )
        # 前次失败导入可能残留无 SKILL.md 的不完整目录：清掉整体重拷，
        # 避免 copytree 抛 FileExistsError，也不与残留内容混合。
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(package_dir, target)
        return ImportOutcome(asset_id=asset_id)

    @staticmethod
    def _read_validated_frontmatter(skill_md: Path, local_id: str) -> dict[str, Any]:
        """读取并校验 SKILL.md frontmatter；缺失/非法按 INVALID_PACKAGE 收敛。"""
        try:
            return _read_frontmatter(skill_md)
        except (OSError, ValueError) as exc:
            raise AssetError(
                AssetErrorCode.INVALID_PACKAGE,
                f"skill {local_id!r} 的 SKILL.md 缺失或无法解析：{exc}",
                details={"id": local_id},
            ) from exc
