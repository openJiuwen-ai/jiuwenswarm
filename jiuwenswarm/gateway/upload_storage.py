# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared naming rules for browser-uploaded attachments.

Documents and images land in the same per-session ``uploads`` directory and
must agree on how a browser-supplied name becomes a path on disk, so the rules
live here instead of being duplicated per attachment kind.
"""

from __future__ import annotations

import re
from pathlib import Path

# Characters no mainstream filesystem accepts inside a name: ASCII control
# chars, POSIX/Windows path separators, and the rest of the Windows-reserved
# punctuation. Everything else — including non-ASCII letters — is kept.
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


def safe_session_dirname(session_id: str | None) -> str:
    """Return the directory name for a session's upload folder."""
    text = str(session_id or "default").strip() or "default"
    return _UNSAFE_SESSION_CHARS.sub("_", text)[:_MAX_SESSION_ID_CHARS]


def unique_upload_path(path: Path) -> Path:
    """Return ``path`` or the first free ``stem-N`` variant beside it."""
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(1, 1000):
        candidate = path.with_name(f"{stem}-{index}{suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{stem}-overflow{suffix}")


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


def _truncate_text_bytes(text: str, max_bytes: int) -> str:
    """Cut ``text`` to ``max_bytes``, never splitting a UTF-8 sequence."""
    return text.encode("utf-8")[:max_bytes].decode("utf-8", "ignore")
