from __future__ import annotations

import math
import os
import posixpath
import re
import shlex
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from jiuwenclaw.agentserver.skilldev.error_codes import (
    ERR_FW_ARCHIVE_COMMAND_BLOCKED,
    ERR_FW_ARCHIVE_RATIO_EXCEEDED,
)

_DEFAULT_MAX_COMPRESSION_RATIO = float(
    os.getenv("SKILLDEV_MAX_ARCHIVE_COMPRESSION_RATIO", "100")
)
_COMMAND_SPLIT_RE = re.compile(r"\s*(?:&&|\|\||;|\r?\n)\s*")
_METACHAR_RE = re.compile(r"[$%*?<>|`]")


class ArchiveSecurityError(ValueError):
    """Raised when an archive fails pre-extraction validation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


@dataclass(frozen=True)
class ArchiveStats:
    format: str
    compressed_bytes: int
    uncompressed_bytes: int
    ratio: float


def inspect_zip_archive(zip_path: Path) -> ArchiveStats:
    compressed_bytes = 0
    uncompressed_bytes = 0

    with zipfile.ZipFile(zip_path, "r") as archive:
        for member in archive.infolist():
            name = member.filename.replace("\\", "/")
            if member.is_dir():
                continue
            if name.startswith("__MACOSX/") or name.startswith("._"):
                continue
            compressed_bytes += max(member.compress_size, 0)
            uncompressed_bytes += max(member.file_size, 0)

    return ArchiveStats(
        format="zip",
        compressed_bytes=compressed_bytes,
        uncompressed_bytes=uncompressed_bytes,
        ratio=_compute_ratio(
            compressed_bytes=compressed_bytes,
            uncompressed_bytes=uncompressed_bytes,
        ),
    )


def inspect_tar_archive(archive_path: Path) -> ArchiveStats:
    compressed_bytes = max(archive_path.stat().st_size, 0)
    uncompressed_bytes = 0

    with tarfile.open(archive_path, "r:*") as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            uncompressed_bytes += max(member.size, 0)

    return ArchiveStats(
        format="tar",
        compressed_bytes=compressed_bytes,
        uncompressed_bytes=uncompressed_bytes,
        ratio=_compute_ratio(
            compressed_bytes=compressed_bytes,
            uncompressed_bytes=uncompressed_bytes,
        ),
    )


def inspect_archive(archive_path: Path) -> ArchiveStats:
    if zipfile.is_zipfile(archive_path):
        return inspect_zip_archive(archive_path)
    if tarfile.is_tarfile(archive_path):
        return inspect_tar_archive(archive_path)
    raise ArchiveSecurityError(
        ERR_FW_ARCHIVE_COMMAND_BLOCKED,
        f"当前仅支持校验可识别的 zip/tar 压缩包，无法识别文件格式：{archive_path.name}",
    )


def validate_archive_before_extract(
    archive_path: Path,
    *,
    max_ratio: float = _DEFAULT_MAX_COMPRESSION_RATIO,
) -> ArchiveStats:
    stats = inspect_archive(archive_path)
    if stats.uncompressed_bytes <= 0:
        return stats

    if stats.ratio > max_ratio:
        raise ArchiveSecurityError(
            ERR_FW_ARCHIVE_RATIO_EXCEEDED,
            (
                f"压缩包 {archive_path.name} 压缩比过高，"
                f"format={stats.format}，"
                f"ratio={_format_ratio(stats.ratio)}，limit={_format_ratio(max_ratio)}。"
                "已阻止直接解压。请先进行安全检查："
                "1) 确认压缩包来源是否可信，解压是否安全；"
                "2) 不要继续执行 unzip/tar 解压命令或使用其他不安全的方式进行解压的操作；"
                "3) 如需继续，请先请求用户确认或改用受控方式处理。"
            ),
        )
    return stats


def validate_zip_archive_before_extract(
    zip_path: Path,
    *,
    max_ratio: float = _DEFAULT_MAX_COMPRESSION_RATIO,
) -> ArchiveStats:
    stats = validate_archive_before_extract(zip_path, max_ratio=max_ratio)
    if stats.format != "zip":
        raise ArchiveSecurityError(
            ERR_FW_ARCHIVE_COMMAND_BLOCKED,
            f"文件 {zip_path.name} 不是合法的 zip 压缩包",
        )
    return stats


def guard_archive_command_before_exec(
    command: str,
    *,
    cwd: str | Path | None = None,
    max_ratio: float = _DEFAULT_MAX_COMPRESSION_RATIO,
) -> None:
    current_cwd = cwd
    for raw_segment in _COMMAND_SPLIT_RE.split(command):
        segment = raw_segment.strip()
        if not segment:
            continue

        current_cwd = _extract_cwd_from_command_segment(segment, current_cwd) or current_cwd
        archive_arg = _extract_archive_arg_from_command(segment)
        if archive_arg is None:
            continue

        archive_path = _resolve_archive_path(
            archive_arg,
            cwd=current_cwd,
        )
        if archive_path is None:
            raise ArchiveSecurityError(
                ERR_FW_ARCHIVE_COMMAND_BLOCKED,
                f"解压命令中的压缩包路径无法静态校验：{archive_arg}",
            )
        if not archive_path.exists():
            raise ArchiveSecurityError(
                ERR_FW_ARCHIVE_COMMAND_BLOCKED,
                f"解压前未找到待校验压缩包：{archive_path}",
            )

        validate_archive_before_extract(archive_path, max_ratio=max_ratio)


def _extract_archive_arg_from_command(command: str) -> str | None:
    tokens = _shell_split(command)
    if not tokens:
        return None

    head = tokens[0].lower()
    if head == "unzip":
        return _first_non_option_token(tokens[1:])

    if head == "tar":
        return _extract_tar_archive_arg(tokens[1:])

    if head == "expand-archive":
        return _value_after_option(tokens[1:], {"-path"})

    if head in {"python", "python3"} and len(tokens) >= 4:
        if tokens[1:3] == ["-m", "zipfile"] and "-e" in tokens[3:]:
            idx = tokens.index("-e")
            if idx + 1 < len(tokens):
                return tokens[idx + 1]

    return None


def _extract_cwd_from_command_segment(command: str, cwd: str | Path | None) -> str | Path | None:
    tokens = _shell_split(command)
    if not tokens:
        return None

    if tokens[0].lower() != "cd":
        return None

    target = _extract_cd_target(tokens[1:])
    if target is None:
        return None
    return _resolve_dir_path(target, cwd=cwd)


def _shell_split(command: str) -> list[str]:
    for posix in (True, False):
        try:
            tokens = shlex.split(command, posix=posix)
        except ValueError:
            continue
        if tokens:
            return tokens
    return []


def _first_non_option_token(tokens: Sequence[str]) -> str | None:
    for token in tokens:
        if not token.startswith("-"):
            return token
    return None


def _value_after_option(tokens: Sequence[str], names: set[str]) -> str | None:
    lower_names = {name.lower() for name in names}
    for idx, token in enumerate(tokens):
        lower = token.lower()
        if lower in lower_names and idx + 1 < len(tokens):
            return tokens[idx + 1]
        for name in lower_names:
            if lower.startswith(f"{name}="):
                return token.split("=", 1)[1]
    return None


def _extract_tar_archive_arg(tokens: Sequence[str]) -> str | None:
    for idx, token in enumerate(tokens):
        lower = token.lower()
        if lower in {"-f", "--file"} and idx + 1 < len(tokens):
            return tokens[idx + 1]
        if lower.startswith("--file="):
            return token.split("=", 1)[1]
        if lower.startswith("-f") and len(token) > 2:
            return token[2:]
        bare_flag = lower.lstrip("-")
        if bare_flag and bare_flag.isalpha() and "f" in bare_flag and idx + 1 < len(tokens):
            return tokens[idx + 1]
    return None


def _extract_cd_target(tokens: Sequence[str]) -> str | None:
    for token in tokens:
        lower = token.lower()
        if lower in {"/d", "--"}:
            continue
        if token.startswith("-"):
            continue
        return token
    return None


def _resolve_archive_path(
    raw_path: str,
    *,
    cwd: str | Path | None,
) -> Path | None:
    candidate = _clean_path_token(raw_path)
    if candidate is None:
        return None

    if isinstance(cwd, Path):
        return (cwd / Path(candidate).expanduser()).resolve()
    if isinstance(cwd, str):
        return Path(_join_posix_path(candidate, cwd))

    try:
        return Path(candidate).expanduser().resolve()
    except OSError:
        return None


def _format_ratio(value: float) -> str:
    if math.isinf(value):
        return "inf"
    return f"{value:.2f}"


def _compute_ratio(*, compressed_bytes: int, uncompressed_bytes: int) -> float:
    if uncompressed_bytes <= 0:
        return 0.0
    if compressed_bytes <= 0:
        return math.inf
    return uncompressed_bytes / compressed_bytes


def _resolve_dir_path(raw_path: str, *, cwd: str | Path | None) -> str | Path | None:
    candidate = _clean_path_token(raw_path)
    if candidate is None:
        return None

    if isinstance(cwd, Path):
        return (cwd / Path(candidate).expanduser()).resolve()
    if isinstance(cwd, str):
        return _join_posix_path(candidate, cwd)

    try:
        return Path(candidate).expanduser().resolve()
    except OSError:
        return None


def _clean_path_token(raw_path: str) -> str | None:
    candidate = raw_path.strip().strip("\"'")
    if not candidate or _METACHAR_RE.search(candidate):
        return None
    return candidate


def _join_posix_path(path_text: str, cwd: str | Path | None) -> str:
    expanded = posixpath.expanduser(path_text)
    if expanded.startswith("/"):
        return posixpath.normpath(expanded)
    base = posixpath.expanduser(cwd) if isinstance(cwd, str) else ""
    return posixpath.normpath(posixpath.join(base, expanded))
