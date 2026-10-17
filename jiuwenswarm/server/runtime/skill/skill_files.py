# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Skill 工作副本文件预览（skills.files.*）."""

from __future__ import annotations

import mimetypes
from pathlib import Path, PurePosixPath
from typing import Any

from jiuwenswarm.server.runtime.skill.archive_store import ARCHIVE_DIRNAME

ERROR_UNSAFE_PATH = "SKILL_UNSAFE_PATH"
ERROR_NOT_FOUND = "SKILL_NOT_FOUND"
ERROR_FILE_TOO_LARGE = "SKILL_FILE_TOO_LARGE"

# 文本预览上限；超限改走 download_url
DEFAULT_TEXT_PREVIEW_MAX_BYTES = 512 * 1024

_TEXT_MIME_PREFIXES = ("text/",)
_TEXT_MIME_EXACT = frozenset(
    {
        "application/json",
        "application/xml",
        "application/javascript",
        "application/x-javascript",
        "application/yaml",
        "application/x-yaml",
        "application/toml",
        "application/sql",
    }
)
_TEXT_SUFFIXES = frozenset(
    {
        ".md",
        ".markdown",
        ".txt",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".xml",
        ".html",
        ".htm",
        ".css",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".py",
        ".sh",
        ".bat",
        ".ps1",
        ".csv",
        ".tsv",
        ".ini",
        ".cfg",
        ".conf",
        ".env",
        ".log",
        ".svg",
    }
)


class SkillFilesError(Exception):
    """文件预览相关稳定业务错误."""

    code: str
    message: str

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def guess_mime_type(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(path.name)
    if guessed:
        return guessed
    if path.suffix.lower() in {".md", ".markdown"}:
        return "text/markdown"
    return "application/octet-stream"


def _is_under_archive(skill_root: Path, path: Path) -> bool:
    archive = (skill_root / ARCHIVE_DIRNAME).resolve()
    try:
        path.resolve().relative_to(archive)
        return True
    except ValueError:
        return False


def _posix_rel(skill_root: Path, path: Path) -> str:
    rel = path.resolve().relative_to(skill_root.resolve())
    return PurePosixPath(*rel.parts).as_posix()


def list_skill_workspace_files(skill_root: Path) -> list[dict[str, Any]]:
    """列出 workspace 工作副本文件树，隐藏根级 ``.archive/``."""
    if not skill_root.is_dir():
        raise SkillFilesError(ERROR_NOT_FOUND, f"Skill 目录不存在: {skill_root}")

    root = skill_root.resolve()
    entries: list[dict[str, Any]] = []

    for path in sorted(root.rglob("*"), key=lambda p: _posix_rel(root, p).lower()):
        if path.is_symlink():
            continue
        try:
            resolved = path.resolve()
            resolved.relative_to(root)
        except ValueError:
            continue
        if _is_under_archive(root, resolved):
            continue
        # 跳过根级 .archive 目录本身
        if resolved == (root / ARCHIVE_DIRNAME).resolve():
            continue

        rel = _posix_rel(root, resolved)
        if not rel or rel == ".":
            continue
        if path.is_dir():
            entries.append(
                {
                    "path": rel,
                    "type": "directory",
                    "size": None,
                    "mime_type": None,
                }
            )
        elif path.is_file():
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            entries.append(
                {
                    "path": rel,
                    "type": "file",
                    "size": int(size),
                    "mime_type": guess_mime_type(path),
                }
            )
    return entries


def _is_absolute_skill_relative_path(raw: str) -> bool:
    """相对路径字段是否实际为绝对路径（POSIX/Windows）."""
    if raw.startswith("/") or raw.startswith("\\"):
        return True
    return len(raw) >= 2 and raw[1] == ":"


def resolve_skill_relative_file(skill_root: Path, relative_path: str) -> tuple[Path, str]:
    """将相对路径安全解析为 workspace 内普通文件."""
    raw = str(relative_path or "").strip()
    if not raw:
        raise SkillFilesError(ERROR_UNSAFE_PATH, "文件路径不能为空")
    if _is_absolute_skill_relative_path(raw):
        raise SkillFilesError(ERROR_UNSAFE_PATH, "不允许绝对路径")
    # 统一分隔符并拒绝 ..
    posix = PurePosixPath(raw.replace("\\", "/"))
    if posix.is_absolute() or ".." in posix.parts or posix.parts[:1] == (ARCHIVE_DIRNAME,):
        raise SkillFilesError(ERROR_UNSAFE_PATH, "文件路径非法或指向保留目录")

    root = skill_root.resolve()
    candidate = (skill_root / Path(*posix.parts)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise SkillFilesError(ERROR_UNSAFE_PATH, "文件路径越出 Skill 根目录") from exc

    if _is_under_archive(root, candidate) or candidate == (root / ARCHIVE_DIRNAME).resolve():
        raise SkillFilesError(ERROR_UNSAFE_PATH, "不允许访问 .archive 保留目录")

    if candidate.is_symlink():
        raise SkillFilesError(ERROR_UNSAFE_PATH, "不允许通过符号链接访问文件")
    # 路径链上任一目录为 symlink 也拒绝
    probe = candidate.parent
    while probe != root and probe != probe.parent:
        if probe.is_symlink():
            raise SkillFilesError(ERROR_UNSAFE_PATH, "不允许通过符号链接访问文件")
        probe = probe.parent

    if not candidate.exists():
        raise SkillFilesError(ERROR_NOT_FOUND, f"文件不存在: {posix.as_posix()}")
    if candidate.is_dir():
        raise SkillFilesError(ERROR_UNSAFE_PATH, "不允许预览目录")
    if not candidate.is_file():
        raise SkillFilesError(ERROR_UNSAFE_PATH, "目标不是普通文件")
    try:
        nlink = int(candidate.stat().st_nlink)
    except OSError as exc:
        raise SkillFilesError(ERROR_UNSAFE_PATH, f"无法校验文件属性: {posix.as_posix()}") from exc
    # 拒绝硬链接
    if nlink > 1:
        raise SkillFilesError(ERROR_UNSAFE_PATH, "不允许通过硬链接访问文件")

    return candidate, posix.as_posix()


def is_text_previewable(path: Path, mime_type: str) -> bool:
    if mime_type.startswith(_TEXT_MIME_PREFIXES) or mime_type in _TEXT_MIME_EXACT:
        return True
    if path.suffix.lower() in _TEXT_SUFFIXES:
        return True
    return False


def read_text_preview(path: Path, *, max_bytes: int = DEFAULT_TEXT_PREVIEW_MAX_BYTES) -> str | None:
    """尝试按 UTF-8 读取文本预览；不可预览返回 None."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise SkillFilesError(ERROR_NOT_FOUND, f"无法读取文件: {path.name}") from exc
    if size > max_bytes:
        return None
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise SkillFilesError(ERROR_NOT_FOUND, f"无法读取文件: {path.name}") from exc
    if b"\x00" in data[:8192]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return data.decode("utf-8-sig")
        except UnicodeDecodeError:
            return None
