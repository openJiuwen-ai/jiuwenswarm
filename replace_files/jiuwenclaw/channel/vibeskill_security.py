# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""
Security helpers for the VibeSkill channel file endpoints.
"""

from __future__ import annotations

import mimetypes
from urllib.parse import unquote


# 文件扩展名黑名单（覆盖 TS-SEC-041 测试集，并加入常见 Windows / Linux 危险格式）
_DANGEROUS_EXTS = frozenset({
    "exe", "sh", "php", "jsp", "bat",  # TS-SEC-041 fixture
    "msi", "scr", "com", "app", "dmg", "apk", "ipa",
    "so", "dll", "dylib", "jar", "war",
    "vbs", "ps1", "cmd", "js", "jse", "wsf",
    "asp", "aspx", "phtml", "phps",
})


# 读取操作禁止访问的绝对路径前缀
_READ_FORBIDDEN = ("/etc", "/proc", "/sys", "/dev", "/root", "/boot", "/var/log")
# 写操作额外禁止（写更严格，覆盖 /bin /usr 等只读系统目录）
_WRITE_FORBIDDEN = _READ_FORBIDDEN + ("/bin", "/sbin", "/usr", "/lib", "/lib64", "/opt")


def validate_file_path(path: str | None, *, operation: str = "read") -> str | None:
    """校验文件路径。

    返回 ``None`` 表示通过；返回错误信息字符串表示拒绝。
    拒绝的场景：
    * 路径为空
    * 包含 ``..`` 段（已先做 URL 解码）
    * 绝对路径命中系统子树黑名单
    """
    if not path or not path.strip():
        return "path is required"
    cleaned = unquote(path).replace("\\", "/").strip()
    for segment in cleaned.split("/"):
        if segment == "..":
            return "path contains '..'"
    if cleaned.startswith("/"):
        prefixes = _WRITE_FORBIDDEN if operation == "write" else _READ_FORBIDDEN
        candidate = cleaned.rstrip("/") or "/"
        for prefix in prefixes:
            if candidate == prefix or candidate.startswith(prefix + "/"):
                return f"path under forbidden prefix {prefix}"
    return None


def is_dangerous_file(filename: str | None) -> str | None:
    """校验文件扩展名。返回 ``None`` 表示通过。"""
    if not filename or not filename.strip():
        return "filename is required"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext in _DANGEROUS_EXTS:
        return f"file extension '.{ext}' is on the blocklist"
    return None


# 常见非标准 / mimetypes 未覆盖的 mime 别名
_MIME_ALIASES = {
    "image/jpg": "image/jpeg",
    "audio/mp3": "audio/mpeg",
    "audio/x-wav": "audio/wav",
    "application/x-zip-compressed": "application/zip",
    "application/x-gzip": "application/gzip",
    "text/x-markdown": "text/markdown",
}

# 扩展名 -> 可接受的 mime（补充 mimetypes 库缺口）
_EXT_TO_MIMES: dict[str, frozenset[str]] = {
    "md": frozenset({"text/markdown", "text/plain", "text/x-markdown"}),
    "markdown": frozenset({"text/markdown", "text/plain", "text/x-markdown"}),
    "py": frozenset({"text/x-python", "text/plain", "application/x-python"}),
    "js": frozenset({"application/javascript", "text/javascript"}),
    "ts": frozenset({"application/typescript", "text/typescript", "video/mp2t"}),
    "csv": frozenset({"text/csv", "application/csv", "text/plain"}),
    "htm": frozenset({"text/html"}),
    "svg": frozenset({"image/svg+xml"}),
    "webp": frozenset({"image/webp"}),
    "tif": frozenset({"image/tiff"}),
    "tiff": frozenset({"image/tiff"}),
    "rar": frozenset({"application/x-rar-compressed", "application/vnd.rar"}),
    "7z": frozenset({"application/x-7z-compressed"}),
    "gz": frozenset({"application/gzip", "application/x-gzip"}),
    "tar": frozenset({"application/x-tar"}),
}


def _normalize_mime(mime: str) -> str:
    cleaned = mime.strip().lower()
    return _MIME_ALIASES.get(cleaned, cleaned)


def _guess_mime_from_filename(filename: str) -> str:
    mime_type, _ = mimetypes.guess_type(filename)
    return _normalize_mime(mime_type or "application/octet-stream")


def _extension_from_filename(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def _mime_matches_extension(mime: str, ext: str) -> bool:
    """判断声明的 mime 是否与扩展名一致。"""
    if not ext:
        return False
    mime_norm = _normalize_mime(mime)
    expected = _guess_mime_from_filename(f"file.{ext}")
    if mime_norm == expected:
        return True
    supplemental = {_normalize_mime(item) for item in _EXT_TO_MIMES.get(ext, ())}
    if mime_norm in supplemental:
        return True
    for candidate in mimetypes.guess_all_extensions(mime_norm, strict=False):
        if candidate.lstrip(".").lower() == ext:
            return True
    for candidate in mimetypes.guess_all_extensions(mime.strip().lower(), strict=False):
        if candidate.lstrip(".").lower() == ext:
            return True
    return False


def validate_mime_extension_match(filename: str, mime: str) -> str | None:
    """校验 mime 与 filename 扩展名是否一致。返回 ``None`` 表示通过。"""
    if not mime or not mime.strip():
        return "mime is required"
    ext = _extension_from_filename(filename)
    if not ext:
        return "filename must include an extension"
    if not _mime_matches_extension(mime, ext):
        return "mime does not match filename extension"
    return None


# 图片扩展名 / mime；这些场景下，content 必须匹配图片 magic
_IMAGE_EXTS = frozenset({"png", "jpg", "jpeg", "gif", "bmp", "webp", "tiff", "svg"})
_IMAGE_MIMES = frozenset({"image/png", "image/jpeg", "image/gif", "image/bmp",
                          "image/webp", "image/tiff", "image/svg+xml"})

# magic number 头几字节 -> 是否为图片
_PNG = b"\x89PNG"
_JPEG = b"\xff\xd8\xff"
_GIF = b"GIF8"
_WEBP = b"RIFF"  # 后面必须跟 WEBP
_BMP = b"BM"
# 可执行文件头
_PE = b"MZ"
_ELF = b"\x7fELF"
_MACHO_LE = b"\xcf\xfa\xed\xfe"
_MACHO_BE = b"\xfe\xed\xfa\xce"
_MACHO_64 = b"\xca\xfe\xba\xbe"
_SHEBANG = b"#!"
_PHP = b"<?php"
_ASP = b"<%"


def _sniff_kind(content: bytes) -> str:
    """基于 magic number 嗅探内容类型。返回 ``"image"`` / ``"executable"`` / ``"unknown"``。"""
    sample = content[:8].lstrip(b"\xef\xbb\xbf")
    if sample.startswith(_PNG) or sample.startswith(_JPEG) or sample.startswith(_GIF):
        return "image"
    if sample.startswith(_WEBP):
        # RIFF + 4 字节 size + WEBP 才是真正的 webp
        return "image" if content[8:12] == b"WEBP" else "unknown"
    if sample.startswith(_BMP):
        return "image"
    if (sample.startswith(_PE) or sample.startswith(_ELF)
            or sample.startswith(_MACHO_LE) or sample.startswith(_MACHO_BE)
            or sample.startswith(_MACHO_64)
            or sample.startswith(_SHEBANG) or sample.startswith(_PHP)
            or sample.startswith(_ASP)):
        return "executable"
    return "unknown"


def validate_file_content(filename: str, mime: str, content: bytes | None) -> str | None:
    """TS-SEC-042：基于内容判断文件类型。

    当扩展名 / mime 声明为图片时，content 必须是图片；否则视为伪造。
    content 为 None 时（只有 URL），跳过内容校验。
    """
    if content is None:
        return None
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    claimed_image = ext in _IMAGE_EXTS or (mime or "").lower() in _IMAGE_MIMES
    kind = _sniff_kind(content)
    if claimed_image and kind != "image":
        return "file content does not match claimed image type"
    if not claimed_image and kind == "executable":
        # 文件名不是可执行类型，但字节是可执行头 → 伪造
        return "file content is an executable but filename does not declare one"
    return None


def validate_file_part(part: object) -> str | None:
    """WebSocket ``type=file`` part 端到端校验。"""
    if not isinstance(part, dict):
        return "file part must be an object"
    filename = str(part.get("filename") or "")
    mime = str(part.get("mime") or "")
    err = is_dangerous_file(filename)
    if err:
        return err
    err = validate_mime_extension_match(filename, mime)
    if err:
        return err
    for url in (part.get("url"), part.get("innerurl"), part.get("innerUrl")):
        if not url:
            continue
        path = str(url)
        if path.startswith("file://"):
            path = path[len("file://"):]
            if not path.startswith("/"):
                slash = path.find("/")
                path = path[slash:] if slash != -1 else "/"
        err = validate_file_path(path, operation="read")
        if err:
            return err
    content = part.get("content")
    if isinstance(content, (bytes, bytearray)):
        return validate_file_content(filename, mime, bytes(content))
    if isinstance(content, str):
        return validate_file_content(filename, mime, content.encode("utf-8", errors="replace"))
    return None
