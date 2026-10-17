# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Skill 正文 Markdown 本地图片改写与受控预览解析."""

from __future__ import annotations

import logging
import mimetypes
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse

from jiuwenswarm.agents.harness.common.tools.web_file_download import (
    PURPOSE_SKILL_CONTENT_IMAGE,
    WebFileDownloadManager,
    generate_skill_content_image_token,
)
from jiuwenswarm.common.utils import get_agent_skills_dir
from jiuwenswarm.server.runtime.skill.archive_store import (
    SkillArchiveError,
    resolve_version_content_root,
)
from jiuwenswarm.server.runtime.skill.skill_files import (
    SkillFilesError,
    guess_mime_type,
    resolve_skill_relative_file,
)

logger = logging.getLogger(__name__)

# Markdown 图片：![alt](url) ；不跨行
_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
# fenced code block（``` / ~~~），改写时跳过，避免误改代码中的图片语法
_MD_FENCE_RE = re.compile(r"(```[\s\S]*?```|~~~[\s\S]*?~~~)")

ALLOWED_SKILL_CONTENT_IMAGE_MIMES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
    }
)

_ALLOWED_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".jfif"})


def is_allowed_skill_content_image(path: Path, mime_type: str | None = None) -> bool:
    """第一版允许的行内预览图片类型（明确禁止 SVG）."""
    mime = (mime_type or guess_mime_type(path) or "").strip().lower()
    if path.suffix.lower() == ".svg" or mime in {"image/svg+xml", "image/svg"}:
        return False
    if mime in ALLOWED_SKILL_CONTENT_IMAGE_MIMES:
        return True
    # jfif 等偶发猜不到精确 MIME
    if path.suffix.lower() in _ALLOWED_IMAGE_SUFFIXES and mime.startswith("image/"):
        return True
    if path.suffix.lower() in _ALLOWED_IMAGE_SUFFIXES and not mime:
        return True
    return False


def _is_external_or_absolute_image_url(raw_url: str) -> bool:
    url = str(raw_url or "").strip()
    if not url:
        return True
    lower = url.lower()
    if lower.startswith(("http://", "https://", "data:", "mailto:", "file:")):
        return True
    if url.startswith("/") or url.startswith("\\"):
        return True
    if len(url) >= 2 and url[1] == ":":
        return True
    parsed = urlparse(url)
    if parsed.scheme in {"http", "https", "data", "mailto", "file"}:
        return True
    return False


def _normalize_markdown_image_target(raw_url: str) -> str | None:
    """提取 Markdown 图片目标中的相对路径；不可改写时返回 None."""
    url = str(raw_url or "").strip()
    if not url:
        return None
    # 允许 title：path "title"
    if url[0] in {'"', "'"}:
        return None
    path_part = url
    # 剥离可选 title（空格 + 引号）
    for q in ('"', "'"):
        idx = path_part.find(f" {q}")
        if idx > 0:
            path_part = path_part[:idx].strip()
            break
    path_part = path_part.strip()
    if _is_external_or_absolute_image_url(path_part):
        return None
    decoded = unquote(path_part)
    decoded = decoded.replace("\\", "/").lstrip("./")
    if not decoded or decoded.startswith("../") or "/../" in f"/{decoded}/":
        return None
    return decoded


def build_skill_content_image_url(token: str, session_id: str) -> str:
    """预览 URL：token + session_id，供 <img> 请求携带当前会话."""
    base = WebFileDownloadManager.generate_download_url(token)
    sid = str(session_id or "").strip()
    if not sid:
        return base
    return f"{base}&session_id={quote(sid, safe='')}"


def _is_invalid_skill_name(skill_name: str) -> bool:
    """Skill 名是否含空/分隔符/越级段."""
    if not skill_name:
        return True
    return "/" in skill_name or "\\" in skill_name or ".." in skill_name


def resolve_skill_content_root(
    *,
    name: str,
    version: str | None,
    skills_dir: Path | None = None,
) -> Path:
    """按 name + version 定位内容根（workspace 或版本副本）."""
    skill_name = str(name or "").strip()
    if _is_invalid_skill_name(skill_name):
        raise SkillFilesError("SKILL_UNSAFE_PATH", "非法 Skill 名称")
    root_dir = Path(skills_dir) if skills_dir is not None else get_agent_skills_dir()
    skill_dir = (root_dir / skill_name).resolve()
    try:
        skill_dir.relative_to(root_dir.resolve())
    except ValueError as exc:
        raise SkillFilesError("SKILL_UNSAFE_PATH", "Skill 路径越界") from exc
    if not skill_dir.is_dir():
        raise SkillFilesError("SKILL_NOT_FOUND", f"未找到 skill: {skill_name}")

    version_raw = version
    if version_raw is None or (isinstance(version_raw, str) and not str(version_raw).strip()):
        return skill_dir
    try:
        return resolve_version_content_root(skill_dir, str(version_raw).strip())
    except SkillArchiveError as exc:
        raise SkillFilesError(exc.code, exc.message) from exc


def resolve_skill_content_image_file(
    *,
    name: str,
    version: str | None,
    relative_path: str,
    skills_dir: Path | None = None,
) -> tuple[Path, str]:
    """服务端重解析图片文件；返回 (绝对路径, mime)."""
    content_root = resolve_skill_content_root(
        name=name, version=version, skills_dir=skills_dir
    )
    file_path, normalized = resolve_skill_relative_file(content_root, relative_path)
    mime = guess_mime_type(file_path)
    if not is_allowed_skill_content_image(file_path, mime):
        raise SkillFilesError("SKILL_INVALID_PACKAGE", f"不支持的图片类型: {normalized}")
    return file_path, mime if mime.startswith("image/") else (
        mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    )


def rewrite_skill_markdown_images(
    content: str,
    *,
    skill_name: str,
    version: str | None,
    content_root: Path,
    session_id: str,
    expires_in: int = 600,
) -> str:
    """将合法本地相对图片改写为受控预览 URL；失败单图降级，不改磁盘.

    跳过 fenced code block，避免改写代码示例中的图片语法。
    """
    text = content if isinstance(content, str) else ""
    if not text or "![" not in text:
        return text
    sid = str(session_id or "").strip()
    if not sid:
        # 无会话则无法签发绑定 sid 的 token；保留原文
        return text

    def _replace(match: re.Match[str]) -> str:
        alt = match.group(1)
        raw_target = match.group(2)
        rel = _normalize_markdown_image_target(raw_target)
        if rel is None:
            return match.group(0)
        try:
            file_path, _mime = resolve_skill_relative_file(content_root, rel)
            mime = guess_mime_type(file_path)
            if not is_allowed_skill_content_image(file_path, mime):
                return match.group(0)
            token = generate_skill_content_image_token(
                name=skill_name,
                version=version,
                relative_path=rel,
                session_id=sid,
                expires_in=expires_in,
            )
            url = build_skill_content_image_url(token, sid)
            return f"![{alt}]({url})"
        except Exception:
            logger.debug(
                "[skill_content_images] skip rewrite skill=%s rel=%s",
                skill_name,
                rel,
                exc_info=True,
            )
            return match.group(0)

    parts = _MD_FENCE_RE.split(text)
    out: list[str] = []
    for idx, part in enumerate(parts):
        # capturing split：奇数段为 fence 本体
        if idx % 2 == 1:
            out.append(part)
        else:
            out.append(_MD_IMAGE_RE.sub(_replace, part))
    return "".join(out)


def extract_request_session_id(
    *,
    query: dict[str, str] | None = None,
    headers: Any = None,
) -> str:
    """从 download 请求中提取当前会话上下文（query / header / cookie）."""
    if query:
        for key in ("session_id", "sid"):
            value = str(query.get(key) or "").strip()
            if value:
                return value
    if headers is not None:
        try:
            header_sid = headers.get("X-Jiuwen-Session-Id") or headers.get(
                "x-jiuwen-session-id"
            )
        except Exception:
            header_sid = None
        if isinstance(header_sid, str) and header_sid.strip():
            return header_sid.strip()
        try:
            cookie = headers.get("Cookie") or headers.get("cookie") or ""
        except Exception:
            cookie = ""
        if isinstance(cookie, str) and cookie:
            for part in cookie.split(";"):
                item = part.strip()
                lower = item.lower()
                if lower.startswith("jiuwen_session_id="):
                    return item.split("=", 1)[1].strip()
                if lower.startswith("session_id="):
                    return item.split("=", 1)[1].strip()
    return ""


def validate_skill_content_image_payload(
    payload: dict[str, Any],
    *,
    request_session_id: str = "",
) -> str | None:
    """校验 skill_content_image token 载荷；失败返回错误码字符串.

    必须存在当前会话，且与 token.sid 一致；无会话一律拒绝。
    """
    if not isinstance(payload, dict):
        return "invalid_or_expired_token"
    if str(payload.get("purpose") or "").strip() != PURPOSE_SKILL_CONTENT_IMAGE:
        return "invalid_or_expired_token"
    if payload.get("path"):
        # 该用途不得携带绝对路径
        return "invalid_or_expired_token"
    token_sid = str(payload.get("sid") or "").strip()
    if not token_sid:
        return "invalid_or_expired_token"
    req_sid = str(request_session_id or "").strip()
    if not req_sid:
        return "invalid_or_expired_token"
    if req_sid != token_sid:
        return "invalid_or_expired_token"
    name = str(payload.get("name") or "").strip()
    relative_path = str(payload.get("relative_path") or "").strip()
    if not name or not relative_path:
        return "invalid_or_expired_token"
    return None
