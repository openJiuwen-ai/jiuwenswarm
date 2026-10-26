# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""上传文件名安全与扩展名黑名单（纯函数，gateway/runtime/server 三方共用）。

从 ``server/runtime/attachments`` 下沉（AgentOS 保留前置：router_client
跨界清零）；原位 re-export 保持兼容。
"""

from __future__ import annotations

import re
from pathlib import Path

_UNSAFE_FILENAME_CHARS = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]+')

# Bidirectional overrides let a name render as a different extension than the
# one on disk (``evil‮gnp.md`` shows up as ``evil.md``), so they are
# stripped even though a filesystem would happily store them.
_BIDI_CONTROL_CHARS = re.compile("[‎‏‪-‮⁦-⁩]")

# Session ids are machine-generated ASCII and name a directory, so they keep
# the stricter allowlist: anything unexpected collapses to an underscore.
_UNSAFE_SESSION_CHARS = re.compile(r"[^A-Za-z0-9._-]+")

_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)

# ext4/APFS/NTFS all cap a single name at 255 bytes. Budget in bytes rather
# than characters: 180 CJK characters are 540 bytes and would be rejected.
_MAX_FILENAME_BYTES = 180
_MAX_SESSION_ID_CHARS = 120


def _sanitize_name(filename: str) -> str:
    """Strip directories and unsafe characters; return "" when nothing is left."""
    name = Path(str(filename or "")).name.strip()
    name = _BIDI_CONTROL_CHARS.sub("", name)
    name = _UNSAFE_FILENAME_CHARS.sub("_", name)
    # Windows silently drops trailing dots and spaces; strip them so the stored
    # name matches what every platform reports back.
    name = name.rstrip(". ").strip()
    if not name or name in {".", ".."}:
        return ""
    if Path(name).stem.upper() in _WINDOWS_RESERVED_NAMES:
        return ""
    return name


def _truncate_text_bytes(text: str, max_bytes: int) -> str:
    """Cut ``text`` to ``max_bytes``, never splitting a UTF-8 sequence."""
    return text.encode("utf-8")[:max_bytes].decode("utf-8", "ignore")


def _truncate_filename_bytes(name: str, max_bytes: int) -> str:
    """Cap a name at ``max_bytes`` UTF-8 bytes, keeping its extension."""
    if len(name.encode("utf-8")) <= max_bytes:
        return name

    suffix = Path(name).suffix
    suffix_bytes = len(suffix.encode("utf-8"))
    if suffix_bytes >= max_bytes:
        return _truncate_text_bytes(name, max_bytes)

    stem = _truncate_text_bytes(Path(name).stem, max_bytes - suffix_bytes)
    return f"{stem}{suffix}" if stem else _truncate_text_bytes(name, max_bytes)


def safe_upload_filename(filename: str, *, fallback: str) -> str:
    """Turn a browser-supplied filename into a safe, still-readable name.

    Only characters a filesystem rejects are replaced, so Unicode names
    (Chinese, Japanese, accented Latin, ...) survive intact — the stored path
    keeps telling both the user and the agent which file it is.

    Args:
        filename: Raw name from the upload payload; directory parts are dropped.
        fallback: Name to use when nothing usable remains (e.g. ``document-1.md``).

    Returns:
        A single path component safe to join onto the uploads directory.
    """
    name = _sanitize_name(filename)
    if not name:
        name = _sanitize_name(fallback) or "upload"
    return _truncate_filename_bytes(name, _MAX_FILENAME_BYTES)


FORBIDDEN_DOCUMENT_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".exe",
        ".dll",
        ".msi",
        ".scr",
        ".bat",
        ".cmd",
        ".ps1",
        ".vbs",
        ".wsf",
        ".hta",
        ".jar",
        ".lnk",
        ".bin",
        ".so",
        ".dylib",
        ".app",
        ".dmg",
        ".pkg",
        ".command",
        ".scpt",
        ".scptd",
        ".workflow",
        ".xpc",
        ".bundle",
        ".framework",
        ".kext",
        ".prefpane",
        ".saver",
        ".component",
    }
)


def forbidden_formats() -> list[str]:
    """Return sorted list of forbidden upload extensions."""
    return sorted(FORBIDDEN_DOCUMENT_EXTENSIONS)


def _document_suffix(filename: str | None) -> str:
    name = Path(str(filename or "")).name
    return Path(name).suffix.lower()


def is_forbidden_document(*, filename: str | None = None, suffix: str | None = None) -> bool:
    """Return True when the file extension is on the upload blacklist."""
    ext = (suffix or _document_suffix(filename) or "").lower()
    if not ext.startswith(".") and ext:
        ext = f".{ext}"
    return ext in FORBIDDEN_DOCUMENT_EXTENSIONS


def safe_session_dirname(session_id: str | None) -> str:
    """Return the directory name for a session's upload folder.

    目录名与文件名用同一套点目录口径（见 ``_sanitize_name``）：``.`` 与 ``..`` 过
    白名单后仍是自身，作为目录名会跳出 sessions 树（``<sessions>/<id>/uploads`` 变成
    ``<agent_root>/uploads``）。Windows 还会静默丢弃名字末尾的点与空格，``"..."``
    因此等价于 ``".."``，所以先按平台真实语义剥尾再判。
    """
    text = str(session_id or "default").strip() or "default"
    name = _UNSAFE_SESSION_CHARS.sub("_", text)[:_MAX_SESSION_ID_CHARS]
    name = name.rstrip(". ").strip()
    if not name or name in {".", ".."}:
        return "default"
    return name


def strip_data_uri_prefix(raw_base64: str) -> str:
    """剥离 ``data:...;base64,`` 前缀。

    某些前端上传库（react-dropzone 配合 FileReader.readAsDataURL）会把文件
    编码成 ``data:application/pdf;base64,JVBERi0xLjQK...`` 形式。``base64.b64decode``
    在 ``validate=True`` 下会因前缀中的 ``:`` ``/`` ``;`` ``,`` 等非字母表字符
    抛 ``binascii.Error`` 被上层 ``suppress`` 吞掉，导致合法文件被当作无效丢弃。
    """
    if raw_base64.startswith("data:"):
        return raw_base64.split(",", 1)[-1]
    return raw_base64

# ``.jpeg`` / ``.jfif`` 与 ``image/jpeg`` 的规范后缀 ``.jpg`` 等价。已有这些后缀时
# 保留原文件名，避免 ``sample.jpeg`` 被再拼成 ``sample.jpeg.jpg``。
_SUPPORTED_IMAGE_MIME_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
_IMAGE_FILENAME_SUFFIX_ALIASES = frozenset({".jpeg", ".jfif"})


def image_suffix_for_mime(mime_type: str) -> str | None:
    """返回受支持图片 MIME 类型对应的规范扩展名（含点），不支持返回 ``None``。"""
    return _SUPPORTED_IMAGE_MIME_TYPES.get(mime_type)


def supported_image_suffixes() -> frozenset[str]:
    """返回已有合法图片扩展名（含点），含 ``.jpeg``/``.jfif`` 等价别名。"""
    return frozenset(_SUPPORTED_IMAGE_MIME_TYPES.values()) | _IMAGE_FILENAME_SUFFIX_ALIASES


def ensure_image_upload_filename(filename: str, canonical_suffix: str) -> str:
    """已有合法图片后缀时保留原名，否则在末尾补上规范后缀。"""
    if Path(filename).suffix.lower() in supported_image_suffixes():
        return filename
    return f"{filename}{canonical_suffix}"
