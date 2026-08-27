# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Skill 本地产品版本仓储（``.archive/versions``）.

版本真值只存放在 Skill 根目录的 ``.archive/versions/index.json``，
不再使用 ``skills_state.json.installed_plugins[].version``。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path, PurePosixPath
from typing import Any

logger = logging.getLogger(__name__)

ARCHIVE_DIRNAME = ".archive"
VERSIONS_DIRNAME = "versions"
INDEX_FILENAME = "index.json"
CONTENT_DIRNAME = "content"
SCHEMA_VERSION = 2

ERROR_VERSION_NOT_FOUND = "SKILL_VERSION_NOT_FOUND"
ERROR_VERSION_CONTENT_INVALID = "SKILL_VERSION_CONTENT_INVALID"
ERROR_INDEX_CORRUPT = "SKILL_VERSION_CONTENT_INVALID"


class SkillArchiveError(Exception):
    """``.archive`` 读写相关的稳定业务错误."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def empty_versions_index() -> dict[str, Any]:
    """无版本本地 Skill 的标准空索引结构."""
    return {
        "schema_version": SCHEMA_VERSION,
        "current_version": None,
        "installed_asset_id": None,
        "versions": [],
        "remote_asset_id": None,
        "last_published_version": None,
        "updated_at": "",
    }


def archive_root(skill_dir: Path) -> Path:
    return skill_dir / ARCHIVE_DIRNAME


def versions_index_path(skill_dir: Path) -> Path:
    return skill_dir / ARCHIVE_DIRNAME / VERSIONS_DIRNAME / INDEX_FILENAME


def version_content_dir(skill_dir: Path, storage_id: str) -> Path:
    return skill_dir / ARCHIVE_DIRNAME / VERSIONS_DIRNAME / CONTENT_DIRNAME / storage_id


def _validate_index_shape(data: dict[str, Any]) -> None:
    if "versions" not in data or not isinstance(data.get("versions"), list):
        raise SkillArchiveError(ERROR_INDEX_CORRUPT, "版本索引缺少 versions 数组")
    current = data.get("current_version")
    if current is not None and not isinstance(current, str):
        raise SkillArchiveError(ERROR_INDEX_CORRUPT, "版本索引 current_version 类型无效")
    installed = data.get("installed_asset_id")
    if installed is not None and not isinstance(installed, str):
        raise SkillArchiveError(ERROR_INDEX_CORRUPT, "版本索引 installed_asset_id 类型无效")
    current_empty = current is None or (isinstance(current, str) and not current.strip())
    installed_empty = installed is None or (isinstance(installed, str) and not installed.strip())
    if current_empty != installed_empty:
        raise SkillArchiveError(
            ERROR_INDEX_CORRUPT,
            "版本索引 current_version 与 installed_asset_id 必须同时为空或同时有值",
        )
    for idx, entry in enumerate(data["versions"]):
        if not isinstance(entry, dict):
            raise SkillArchiveError(ERROR_INDEX_CORRUPT, f"versions[{idx}] 必须是对象")
        for key in ("version", "storage_id", "source", "created_at", "updated_at"):
            if not str(entry.get(key) or "").strip():
                raise SkillArchiveError(ERROR_INDEX_CORRUPT, f"versions[{idx}] 缺少字段: {key}")


def read_versions_index(skill_dir: Path) -> dict[str, Any] | None:
    """读取版本索引；文件不存在返回 None；损坏则抛出 SkillArchiveError."""
    path = versions_index_path(skill_dir)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("读取版本索引失败: path=%s error=%s", path, exc)
        raise SkillArchiveError(ERROR_INDEX_CORRUPT, "版本索引损坏或无法解析") from exc
    if not isinstance(raw, dict):
        raise SkillArchiveError(ERROR_INDEX_CORRUPT, "版本索引根节点必须是对象")
    _validate_index_shape(raw)
    return raw


def get_current_version(skill_dir: Path | None) -> str | None:
    """读取 ``current_version``；无索引或空值返回 None。索引损坏时向上抛出."""
    if skill_dir is None or not skill_dir.is_dir():
        return None
    index = read_versions_index(skill_dir)
    if index is None:
        return None
    current = index.get("current_version")
    if isinstance(current, str) and current.strip():
        return current.strip()
    return None


def find_version_entry(index: dict[str, Any], version: str) -> dict[str, Any] | None:
    target = str(version or "").strip()
    if not target:
        return None
    for entry in index.get("versions") or []:
        if isinstance(entry, dict) and str(entry.get("version") or "").strip() == target:
            return entry
    return None


def resolve_version_content_root(skill_dir: Path, version: str) -> Path:
    """解析指定产品版本的完整副本根目录；缺失时抛出 SKILL_VERSION_NOT_FOUND."""
    index = read_versions_index(skill_dir)
    if index is None:
        raise SkillArchiveError(ERROR_VERSION_NOT_FOUND, f"未找到本地版本: {version}")
    entry = find_version_entry(index, version)
    if entry is None:
        raise SkillArchiveError(ERROR_VERSION_NOT_FOUND, f"未找到本地版本: {version}")
    storage_id = str(entry.get("storage_id") or "").strip()
    content_root = version_content_dir(skill_dir, storage_id)
    if not content_root.is_dir():
        raise SkillArchiveError(
            ERROR_VERSION_NOT_FOUND,
            f"版本副本缺失: {version}",
        )
    # 防御路径逃逸：content 根必须落在 .archive/versions/content 下
    try:
        content_root.resolve().relative_to(
            (skill_dir / ARCHIVE_DIRNAME / VERSIONS_DIRNAME / CONTENT_DIRNAME).resolve()
        )
    except ValueError as exc:
        raise SkillArchiveError(ERROR_VERSION_NOT_FOUND, f"版本副本路径非法: {version}") from exc
    return content_root


def build_versions_list_payload(skill_name: str, skill_dir: Path) -> dict[str, Any]:
    """构造 ``skills.versions.list`` 业务载荷."""
    index = read_versions_index(skill_dir)
    if index is None:
        return {
            "success": True,
            "name": skill_name,
            "default_version": None,
            "versions": [],
        }

    current = index.get("current_version")
    default_version = current.strip() if isinstance(current, str) and current.strip() else None
    entries = [e for e in (index.get("versions") or []) if isinstance(e, dict)]

    def _sort_key(entry: dict[str, Any]) -> str:
        return str(entry.get("created_at") or "")

    entries_sorted = sorted(entries, key=_sort_key, reverse=True)

    versions_out: list[dict[str, Any]] = []
    default_seen = False
    for entry in entries_sorted:
        ver = str(entry.get("version") or "").strip()
        if not ver:
            continue
        storage_id = str(entry.get("storage_id") or "").strip()
        content_root = version_content_dir(skill_dir, storage_id) if storage_id else None
        available = bool(content_root and content_root.is_dir() and (
            (content_root / "SKILL.md").is_file()
            or any(content_root.glob("*.md"))
        ))
        is_default = default_version is not None and ver == default_version
        if is_default:
            default_seen = True
        versions_out.append(
            {
                "version": ver,
                "is_default": is_default,
                "source": str(entry.get("source") or "skillhub"),
                "available": available,
                "created_at": str(entry.get("created_at") or ""),
                "updated_at": str(entry.get("updated_at") or ""),
            }
        )

    if not versions_out:
        default_version = None
    elif default_version is not None and not default_seen:
        raise SkillArchiveError(
            ERROR_INDEX_CORRUPT,
            f"默认版本 {default_version} 不在 versions[] 中",
        )
    else:
        # 保证至多一个 is_default=true
        if default_version is not None:
            matched = [v for v in versions_out if v["version"] == default_version]
            if matched:
                for v in versions_out:
                    v["is_default"] = v["version"] == default_version

    return {
        "success": True,
        "name": skill_name,
        "default_version": default_version,
        "versions": versions_out,
    }


def compute_content_checksum(content_root: Path) -> str:
    """对版本副本业务内容计算 sha256（排除嵌套 ``.archive``）."""
    import hashlib

    h = hashlib.sha256()
    if not content_root.is_dir():
        return h.hexdigest()
    root = content_root.resolve()
    files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            path.resolve().relative_to(root)
        except ValueError:
            continue
        if ARCHIVE_DIRNAME in path.resolve().relative_to(root).parts:
            continue
        files.append(path)
    for path in sorted(files, key=lambda p: PurePosixPath(*p.resolve().relative_to(root).parts).as_posix()):
        rel = PurePosixPath(*path.resolve().relative_to(root).parts).as_posix()
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(path.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """原子写 JSON：同目录临时文件 + replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def write_versions_index(skill_dir: Path, index: dict[str, Any]) -> None:
    """校验并原子写回版本索引."""
    _validate_index_shape(index)
    from datetime import datetime, timezone

    index = dict(index)
    index["schema_version"] = int(index.get("schema_version") or SCHEMA_VERSION)
    index["updated_at"] = datetime.now(timezone.utc).isoformat()
    atomic_write_json(versions_index_path(skill_dir), index)


def touch_version_metadata(skill_dir: Path, version: str) -> None:
    """重算指定版本副本 checksum 并更新 ``updated_at``，不改变 current_version."""
    from datetime import datetime, timezone

    index = read_versions_index(skill_dir)
    if index is None:
        raise SkillArchiveError(ERROR_VERSION_NOT_FOUND, f"未找到本地版本: {version}")
    entry = find_version_entry(index, version)
    if entry is None:
        raise SkillArchiveError(ERROR_VERSION_NOT_FOUND, f"未找到本地版本: {version}")
    storage_id = str(entry.get("storage_id") or "").strip()
    content_root = version_content_dir(skill_dir, storage_id)
    if not content_root.is_dir():
        raise SkillArchiveError(ERROR_VERSION_NOT_FOUND, f"版本副本缺失: {version}")
    now = datetime.now(timezone.utc).isoformat()
    entry["checksum_sha256"] = compute_content_checksum(content_root)
    entry["updated_at"] = now
    # 写回 versions 数组中的同一对象引用
    write_versions_index(skill_dir, index)


def write_skillhub_first_install_index(
    skill_dir: Path,
    *,
    version: str,
    asset_id: str,
    storage_id: str,
    checksum_sha256: str,
) -> None:
    """SkillHub 首次安装：写入唯一产品版本与安装资产身份."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    ver = str(version or "").strip()
    aid = str(asset_id or "").strip()
    sid = str(storage_id or "").strip()
    if not ver or not aid or not sid:
        raise SkillArchiveError(ERROR_INDEX_CORRUPT, "首次安装索引缺少 version/asset_id/storage_id")
    index = {
        "schema_version": SCHEMA_VERSION,
        "current_version": ver,
        "installed_asset_id": aid,
        "versions": [
            {
                "version": ver,
                "storage_id": sid,
                "source": "skillhub",
                "checksum_sha256": str(checksum_sha256 or ""),
                "created_at": now,
                "updated_at": now,
            }
        ],
        "remote_asset_id": None,
        "last_published_version": None,
        "updated_at": now,
    }
    write_versions_index(skill_dir, index)


def update_publish_remote_metadata(
    skill_dir: Path,
    *,
    remote_asset_id: str,
    last_published_version: str,
) -> None:
    """发布成功后只更新远端发布字段，不改 current_version / installed_asset_id."""
    index = read_versions_index(skill_dir)
    if index is None:
        index = empty_versions_index()
    else:
        index = dict(index)
        index["versions"] = [
            dict(entry) if isinstance(entry, dict) else entry
            for entry in (index.get("versions") or [])
        ]
    index["remote_asset_id"] = str(remote_asset_id or "").strip() or None
    index["last_published_version"] = str(last_published_version or "").strip() or None
    write_versions_index(skill_dir, index)


def get_installed_asset_id(skill_dir: Path | None) -> str | None:
    """读取 ``installed_asset_id``；无索引返回 None."""
    if skill_dir is None or not skill_dir.is_dir():
        return None
    try:
        index = read_versions_index(skill_dir)
    except SkillArchiveError:
        return None
    if index is None:
        return None
    installed = index.get("installed_asset_id")
    if isinstance(installed, str) and installed.strip():
        return installed.strip()
    return None


def get_last_published_version(skill_dir: Path | None) -> str | None:
    """读取 ``last_published_version``；无索引返回 None."""
    if skill_dir is None or not skill_dir.is_dir():
        return None
    try:
        index = read_versions_index(skill_dir)
    except SkillArchiveError:
        return None
    if index is None:
        return None
    last = index.get("last_published_version")
    if isinstance(last, str) and last.strip():
        return last.strip()
    return None
