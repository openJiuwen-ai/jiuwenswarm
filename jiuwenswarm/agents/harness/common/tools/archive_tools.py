# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Safe archive inspection and extraction tools for ZIP and TAR archives.

Provides cross-platform inspection (inspect_archive) to preview archive contents,
file counts, and uncompressed sizes without disk extraction, plus secure extraction
(extract_archive) with strict path traversal and zip-bomb prevention. Implemented
with Python's standard zipfile and tarfile modules (zero external dependencies).
"""

from __future__ import annotations

import asyncio
import logging
import os
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from openjiuwen.core.foundation.tool import tool

logger = logging.getLogger(__name__)

_MAX_EXTRACTION_FILES = 2000
_MAX_EXTRACTION_BYTES = 100 * 1024 * 1024  # 100 MB


@dataclass(frozen=True)
class ArchiveMemberInfo:
    """Normalized metadata for a single entry inside an archive."""

    name: str
    size: int
    is_dir: bool


def _is_suspicious_entry(name: str) -> bool:
    """Check if entry contains directory traversal segments or absolute paths."""
    posix_parts = PurePosixPath(name).parts
    win_parts = PureWindowsPath(name).parts
    return ".." in posix_parts or ".." in win_parts or os.path.isabs(name)


def _get_workspace() -> str | None:
    """Safely obtain runtime workspace path if available."""
    try:
        # pylint: disable-next=import-outside-toplevel
        from openjiuwen.core.sys_operation.cwd import get_workspace

        return get_workspace()
    except Exception:  # pylint: disable=broad-exception-caught
        return None


def _resolve_path_safely(
    value: str,
    must_exist: bool = True,
    enforce_workspace: bool = False,
) -> Path:
    """Resolve path against runtime workspace or filesystem."""
    text = (value or "").strip()
    if not text:
        raise ValueError("Path cannot be empty")
    path = Path(text).expanduser()
    workspace = _get_workspace()

    if not path.is_absolute():
        if not workspace:
            raise ValueError("Runtime workspace is unavailable")
        path = Path(workspace) / path

    path = path.resolve()

    if enforce_workspace and workspace:
        ws_path = Path(workspace).resolve()
        if not path.is_relative_to(ws_path):
            raise ValueError(f"Path is outside runtime workspace: {path}")

    if must_exist and not path.exists():
        raise FileNotFoundError(f"Path does not exist: {path}")
    return path


def _format_size(size_bytes: int) -> str:
    """Format bytes into a human-readable size string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


def _is_zip_file(path: Path) -> bool:
    """Determine whether path is a ZIP archive."""
    return path.suffix.lower() == ".zip" or zipfile.is_zipfile(str(path))


def _inspect_zip(path: Path) -> list[ArchiveMemberInfo]:
    """Extract member metadata from a ZIP archive."""
    members: list[ArchiveMemberInfo] = []
    with zipfile.ZipFile(str(path), "r") as zf:
        for info in zf.infolist():
            members.append(
                ArchiveMemberInfo(
                    name=info.filename,
                    size=info.file_size,
                    is_dir=info.is_dir(),
                )
            )
    return members


def _inspect_tar(path: Path) -> list[ArchiveMemberInfo]:
    """Extract member metadata from a TAR archive."""
    members: list[ArchiveMemberInfo] = []
    with tarfile.open(str(path), "r:*") as tf:
        for info in tf.getmembers():
            members.append(
                ArchiveMemberInfo(
                    name=info.name,
                    size=info.size,
                    is_dir=info.isdir(),
                )
            )
    return members


def _summarize_members(
    members: list[ArchiveMemberInfo],
) -> tuple[int, int, int, list[str]]:
    """Aggregate file counts, directory counts, uncompressed bytes, and suspicious names."""
    total_files = 0
    total_dirs = 0
    total_uncompressed = 0
    suspicious: list[str] = []
    for member in members:
        if member.is_dir:
            total_dirs += 1
        else:
            total_files += 1
            total_uncompressed += member.size
        if _is_suspicious_entry(member.name):
            suspicious.append(member.name)
    return total_files, total_dirs, total_uncompressed, suspicious


def _inspect_archive_sync(archive_path_str: str, max_items: int) -> str:
    """Synchronously inspect archive file structure and metadata."""
    path = _resolve_path_safely(archive_path_str, must_exist=True)
    if not path.is_file():
        raise ValueError(f"archive_path is not a file: {path}")

    is_zip = _is_zip_file(path)
    try:
        members = _inspect_zip(path) if is_zip else _inspect_tar(path)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        raise ValueError(f"Failed to open archive '{path.name}': {exc}") from exc

    total_files, total_dirs, total_uncompressed, suspicious = _summarize_members(members)
    lines: list[str] = [
        f"**Archive: {path.name}**",
        f"- Format: {'ZIP' if is_zip else 'TAR'}",
        f"- Files: {total_files}, Directories: {total_dirs}",
        f"- Compressed Size: {_format_size(path.stat().st_size)}",
        f"- Uncompressed Size: {_format_size(total_uncompressed)}",
    ]
    if suspicious:
        lines.append(
            f"\n> Note: {len(suspicious)} entries contain relative or absolute "
            "path segments. Safe extraction will skip them."
        )
    lines.append(f"\n**Contents (showing up to {max_items} items):**")
    for idx, member in enumerate(members):
        if idx >= max_items:
            lines.append(f"- ... [{len(members) - max_items} more items omitted]")
            break
        prefix = "📁 " if member.is_dir else "📄 "
        size_str = "" if member.is_dir else f" ({_format_size(member.size)})"
        lines.append(f"- {prefix}`{member.name}`{size_str}")
    return "\n".join(lines)


def _check_extraction_limits(total_bytes: int, file_count: int) -> None:
    """Raise ValueError if extraction byte or file count limits are exceeded."""
    if total_bytes > _MAX_EXTRACTION_BYTES:
        raise ValueError(
            f"Extraction aborted: uncompressed size exceeds limit of "
            f"{_format_size(_MAX_EXTRACTION_BYTES)}"
        )
    if file_count >= _MAX_EXTRACTION_FILES:
        raise ValueError(
            f"Extraction aborted: file count exceeds limit of {_MAX_EXTRACTION_FILES}"
        )


def _safe_extract_zip(zf: zipfile.ZipFile, target_dir: Path) -> list[str]:
    """Safely extract ZIP entries preventing path traversal and zip bombs."""
    extracted_files: list[str] = []
    total_extracted_bytes = 0

    for member in zf.infolist():
        if _is_suspicious_entry(member.filename):
            logger.warning(
                "[extract_archive] Skipping suspicious zip entry: %s",
                member.filename,
            )
            continue

        dest_path = (target_dir / member.filename).resolve()
        if not dest_path.is_relative_to(target_dir):
            logger.warning("[extract_archive] Skipping unsafe entry: %s", member.filename)
            continue

        total_extracted_bytes += member.file_size
        _check_extraction_limits(total_extracted_bytes, len(extracted_files))

        zf.extract(member, str(target_dir))
        if not member.is_dir():
            extracted_files.append(str(dest_path.relative_to(target_dir)))
    return extracted_files


def _is_safe_tar_link(member: tarfile.TarInfo, dest_path: Path, target_dir: Path) -> bool:
    """Verify TAR symlink or hardlink stays within target_dir."""
    if not (member.issym() or member.islnk()):
        return True
    link_target = member.linkname
    if _is_suspicious_entry(link_target):
        logger.warning("[extract_archive] Skipping escaping link: %s", member.name)
        return False
    resolved_link = (dest_path.parent / link_target).resolve()
    if not resolved_link.is_relative_to(target_dir):
        logger.warning(
            "[extract_archive] Skipping link pointing outside target: %s",
            member.name,
        )
        return False
    return True


def _extract_tar_member(
    tf: tarfile.TarFile,
    member: tarfile.TarInfo,
    target_dir: Path,
) -> bool:
    """Extract a single TAR member using data_filter when available."""
    try:
        if hasattr(tarfile, "data_filter"):
            tf.extract(member, str(target_dir), filter="data")
        else:
            tf.extract(member, str(target_dir))
        return True
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning(
            "[extract_archive] Filter error extracting %s: %s, skipping",
            member.name,
            exc,
        )
        return False


def _safe_extract_tar(tf: tarfile.TarFile, target_dir: Path) -> list[str]:
    """Safely extract TAR entries preventing path traversal and unsafe links."""
    extracted_files: list[str] = []
    total_extracted_bytes = 0

    for member in tf.getmembers():
        if _is_suspicious_entry(member.name):
            logger.warning("[extract_archive] Skipping suspicious tar entry: %s", member.name)
            continue

        dest_path = (target_dir / member.name).resolve()
        if not dest_path.is_relative_to(target_dir):
            logger.warning("[extract_archive] Skipping unsafe entry: %s", member.name)
            continue

        if not _is_safe_tar_link(member, dest_path, target_dir):
            continue

        total_extracted_bytes += member.size
        _check_extraction_limits(total_extracted_bytes, len(extracted_files))

        if not _extract_tar_member(tf, member, target_dir):
            continue

        if not member.isdir():
            extracted_files.append(str(dest_path.relative_to(target_dir)))
    return extracted_files


def _extract_archive_sync(archive_path_str: str, target_dir_str: str | None) -> str:
    """Synchronously extract archive contents safely."""
    archive_path = _resolve_path_safely(archive_path_str, must_exist=True)
    if not archive_path.is_file():
        raise ValueError(f"archive_path is not a file: {archive_path}")

    if target_dir_str and target_dir_str.strip():
        target_dir = _resolve_path_safely(
            target_dir_str, must_exist=False, enforce_workspace=True
        )
    else:
        folder_name = archive_path.stem
        if folder_name.endswith(".tar"):
            folder_name = folder_name[:-4]
        target_dir = _resolve_path_safely(
            str(archive_path.parent / f"{folder_name}_extracted"),
            must_exist=False,
            enforce_workspace=True,
        )

    target_dir.mkdir(parents=True, exist_ok=True)
    is_zip = _is_zip_file(archive_path)

    try:
        if is_zip:
            with zipfile.ZipFile(str(archive_path), "r") as zf:
                extracted = _safe_extract_zip(zf, target_dir)
        else:
            with tarfile.open(str(archive_path), "r:*") as tf:
                extracted = _safe_extract_tar(tf, target_dir)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        raise ValueError(f"Extraction failed: {exc}") from exc

    preview_count = min(15, len(extracted))
    preview_lines = [f"- `{f}`" for f in extracted[:preview_count]]
    if len(extracted) > preview_count:
        preview_lines.append(f"- ... and {len(extracted) - preview_count} more files")

    return (
        f"**Extraction Successful**\n"
        f"- Target Directory: `{target_dir}`\n"
        f"- Total Files Extracted: {len(extracted)}\n\n"
        f"**Extracted Files:**\n" + "\n".join(preview_lines)
    )


@tool(
    name="inspect_archive",
    description=(
        "Inspect contents of an archive file (.zip, .tar, .tar.gz, .tgz, .bz2) without extracting. "
        "Returns the file tree, file count, and compressed/uncompressed sizes. "
        "Use this tool when a local archive path is provided and you need to preview structure. "
        "Input: archive_path (local file path), optional max_items (default 100, up to 500)."
    ),
)
async def inspect_archive(
    archive_path: str,
    max_items: int = 100,
) -> str:
    """Inspect archive structure and metadata without extracting to disk."""
    try:
        try:
            safe_max = int(max_items)
        except (TypeError, ValueError):
            safe_max = 100
        safe_max = max(1, min(safe_max, 500))

        logger.info("[inspect_archive] path=%s max_items=%s", archive_path, safe_max)
        return await asyncio.to_thread(_inspect_archive_sync, archive_path, safe_max)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        return f"[ERROR]: inspect_archive failed: {exc}"


@tool(
    name="extract_archive",
    description=(
        "Safely extract an archive file (.zip, .tar, .tar.gz, .tgz, .bz2) to a target directory. "
        "Includes strict path traversal prevention, symlink protection, and zip-bomb limits. "
        "Use this tool when archive contents need to be unpacked onto the filesystem. "
        "Input: archive_path (local file path), optional target_dir (destination folder path, "
        "defaults to <archive_name>_extracted)."
    ),
)
async def extract_archive(
    archive_path: str,
    target_dir: str | None = None,
) -> str:
    """Safely extract an archive file to a target directory on the filesystem."""
    try:
        logger.info("[extract_archive] path=%s target_dir=%s", archive_path, target_dir)
        return await asyncio.to_thread(_extract_archive_sync, archive_path, target_dir)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        return f"[ERROR]: extract_archive failed: {exc}"
