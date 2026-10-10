# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""User prompt helpers for message attachments and multimodal images."""

from __future__ import annotations

import base64
import copy
import json
import mimetypes
import ntpath
import posixpath
import re
from contextvars import ContextVar
from pathlib import Path
from typing import Any

IMAGE_CONTENT_OMITTED = (
    "[Image content omitted from chat-model context. Use the original image "
    "path or a vision tool when image analysis is required.]"
)

_IMAGE_CONTENT_TYPES = frozenset({"image", "image_url", "input_image"})
_VISION_INLINE_IMAGE_MIME_TYPES = frozenset({
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
})
_MAX_VISION_INLINE_IMAGE_BYTES = 10 * 1024 * 1024
_MULTIMODAL_IMAGE_WINDOW_MUTATOR_ATTR = "_jiuwenswarm_multimodal_image_window_mutator"
_CURRENT_MULTIMODAL_IMAGE_FILES: ContextVar[tuple[dict[str, Any], ...]] = ContextVar(
    "jiuwenswarm_current_multimodal_image_files",
    default=(),
)
_INJECTED_PROTOCOL_SUFFIX = re.compile(
    r"\r?\n\r?\n(?:"
    r"<claw_workspace>(?P<workspace>[^\r\n]*)</claw_workspace>\r?\n【工作空间】"
    r"|<claw_cron_create></claw_cron_create>\r?\n【定时任务】"
    r"|<claw_time>[^\r\n]*</claw_time>\r?\n【当前时间】"
    r")[^\r\n]*\s*\Z"
)


def is_image_content_block(part: Any) -> bool:
    if not isinstance(part, dict):
        return False
    block_type = str(part.get("type") or "").strip().lower()
    if block_type in _IMAGE_CONTENT_TYPES:
        return True
    return "image_url" in part or "image" in part


def extract_current_turn_attachments(params: Any) -> list[dict[str, Any]]:
    """Normalize files supplied with this request, without consulting history."""

    if not isinstance(params, dict):
        return []

    candidates: list[Any] = []
    raw_media_items = params.get("media_items")
    if isinstance(raw_media_items, list):
        candidates.extend(raw_media_items)

    files = params.get("files")
    if isinstance(files, list):
        candidates.extend(files)
    elif isinstance(files, dict):
        for key in ("uploaded_images", "uploaded_documents"):
            uploaded = files.get(key)
            if isinstance(uploaded, list):
                candidates.extend(uploaded)

    attachments = params.get("attachments")
    if isinstance(attachments, list):
        candidates.extend(attachments)

    query = params.get("query") or params.get("content")
    if isinstance(query, str):
        parsed = _parse_desktop_context(query)
        if parsed:
            _prefix, context, _body = parsed
            if isinstance(context.get("files"), list):
                candidates.extend(context["files"])

    normalized: list[dict[str, Any]] = []
    by_path: dict[str, dict[str, Any]] = {}
    for item in candidates:
        if not isinstance(item, dict):
            continue
        path = _attachment_string(item, "original_path", "path", "mediaPath")
        if not path:
            continue
        mime_type = _attachment_string(item, "mime_type", "mimeType").lower()
        item_type = _attachment_string(item, "type").lower()
        if not mime_type and "/" in item_type:
            mime_type = item_type
        mime_type = mime_type or mimetypes.guess_type(path)[0] or ""
        attachment: dict[str, Any] = {
            "filename": _attachment_string(item, "filename", "name") or ntpath.basename(path),
            "path": path,
            "mime_type": mime_type,
        }
        text_path = _attachment_string(item, "text_path")
        if text_path:
            attachment["text_path"] = text_path
        if isinstance(item.get("size_bytes"), int):
            attachment["size_bytes"] = item["size_bytes"]
        parser = _attachment_string(item, "parser")
        if parser:
            attachment["parser"] = parser
        if isinstance(item.get("text_truncated"), bool):
            attachment["text_truncated"] = item["text_truncated"]

        # Windows paths compare case-insensitively even on a Linux server.
        path_key = (
            ntpath.normcase(ntpath.normpath(path))
            if ntpath.splitdrive(path)[0] or "\\" in path
            else posixpath.normpath(path)
        )
        existing = by_path.get(path_key)
        if existing is not None:
            if existing["filename"] == ntpath.basename(existing["path"]):
                existing["filename"] = attachment["filename"]
            for key, value in attachment.items():
                if key not in existing or existing[key] == "":
                    existing[key] = value
            continue
        by_path[path_key] = attachment
        normalized.append(attachment)
    return normalized


def _attachment_string(item: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _without_leading_system_reminders(text: str) -> str:
    """Keep user content after runtime reminders, including plan-mode prefixes."""
    text = text.lstrip()
    while text.startswith("<system-reminder>"):
        _reminder, closing_tag, remainder = text.partition("</system-reminder>")
        if not closing_tag:
            return ""
        text = remainder.lstrip()
    return text


def _parse_desktop_context(query: str) -> tuple[str, dict[str, Any], str] | None:
    """Parse a valid desktop carrier at the start, keeping its prefix and body."""
    text = _without_leading_system_reminders(query)
    match = re.match(
        r"^((?:/skill\s+\S+\s+)*)<claw_context>([^\r\n]*)</claw_context>", text,
    )
    if not match:
        return None
    try:
        context = json.loads(match.group(2))
    except (ValueError, TypeError):
        return None
    if not isinstance(context, dict):
        return None
    prefix = query[:len(query) - len(text)] + match.group(1)
    return prefix, context, text[match.end():]


def remove_desktop_attachment_metadata(query: str) -> str:
    """Remove desktop files after binding them to the current user message."""
    parsed = _parse_desktop_context(query)
    if not parsed:
        return query
    prefix, context, body = parsed
    if not isinstance(context.get("files"), list):
        return query
    remaining = dict(context)
    remaining_files = remaining_legacy_file_metadata(context["files"])
    if remaining_files:
        remaining["files"] = remaining_files
    else:
        remaining.pop("files")
    if not remaining:
        return prefix + body.lstrip("\r\n")
    return (
        prefix + "<claw_context>"
        + json.dumps(remaining, ensure_ascii=False, separators=(",", ":"))
        + "</claw_context>" + body
    )


def remaining_legacy_file_metadata(files: Any) -> Any:
    """Keep file metadata not represented by the normalized attachment list."""
    if isinstance(files, list):
        return [item for item in files if not extract_current_turn_attachments({"files": [item]})]
    if isinstance(files, dict):
        remaining = dict(files)
        for key in ("uploaded_images", "uploaded_documents"):
            if isinstance(files.get(key), list):
                records = remaining_legacy_file_metadata(files[key])
                if records:
                    remaining[key] = records
                else:
                    remaining.pop(key)
        return remaining
    return files


def _split_injected_protocol_suffix(query: str) -> tuple[str, str]:
    """Separate recognized channel suffixes while preserving their exact text."""
    body = query
    while match := _INJECTED_PROTOCOL_SUFFIX.search(body):
        workspace_payload = match.group("workspace")
        if workspace_payload is not None:
            try:
                workspace = json.loads(workspace_payload)
            except (ValueError, TypeError):
                break
            if not isinstance(workspace, dict) or not _attachment_string(workspace, "path"):
                break
        body = body[:match.start()]
    return body, query[len(body):]


def extract_image_tool_question(query: str) -> str:
    """Keep user text, excluding valid protocol prefixes and injected suffixes."""
    parsed = _parse_desktop_context(query)
    question = (parsed[2] if parsed else query).strip()
    return _split_injected_protocol_suffix(question)[0].rstrip()


def bind_attachment_context(query: str, attachment_context: str) -> str:
    """Keep attachments next to user text, before channel-injected instructions."""
    if not attachment_context:
        return query
    body, suffix = _split_injected_protocol_suffix(query)
    return body + attachment_context + suffix


def render_current_turn_attachments(attachments: list[dict[str, Any]]) -> str:
    """Describe attachment ownership in the same user message as its query."""
    if not attachments:
        return ""
    return (
        "\n\n以下附件由用户在本轮消息中提供。\n"
        "用户对文件的指代未明确指定对象时，默认从本轮附件中解析。\n"
        "用户明确指定的文件名、路径或历史文件优先。\n"
        "本轮有多个附件时，根据用户要求确定处理范围；目标仍有歧义时，先澄清。\n"
        "附件字段：path 为原文件路径，text_path 为解析文本路径（如有）。\n"
        "<attachments>\n"
        + json.dumps(attachments, ensure_ascii=False, separators=(",", ":"))
        + "\n</attachments>"
    )


def extract_multimodal_image_files(params: Any) -> list[dict[str, Any]]:
    """Use the same request attachments for image input and path context."""
    return _normalize_image_files(extract_current_turn_attachments(params))


def set_current_multimodal_image_files(image_files: list[dict[str, Any]]) -> Any:
    """Bind request image files to the current Core model-call task."""

    return _CURRENT_MULTIMODAL_IMAGE_FILES.set(tuple(_normalize_image_files(image_files)))


def reset_current_multimodal_image_files(token: Any) -> None:
    _CURRENT_MULTIMODAL_IMAGE_FILES.reset(token)


def current_multimodal_image_files() -> list[dict[str, Any]]:
    return list(_CURRENT_MULTIMODAL_IMAGE_FILES.get())


def prepare_multimodal_image_messages(
    messages: list[Any],
    image_files: list[dict[str, Any]] | None = None,
) -> tuple[list[Any], int]:
    images = _normalize_image_files(
        image_files if image_files is not None else current_multimodal_image_files()
    )
    if not images:
        return messages, 0

    target_index = _latest_user_message_index(messages)
    if target_index < 0:
        return messages, 0

    message = messages[target_index]
    content = (
        message.get("content")
        if isinstance(message, dict)
        else getattr(message, "content", None)
    )
    updated_content, injected = _append_image_files_to_content(content, images)
    if not injected:
        return messages, 0

    updated_messages = list(messages)
    updated_messages[target_index] = _copy_message_with_content(message, updated_content)
    return updated_messages, injected


def strip_image_content_blocks(content: Any) -> tuple[Any, int]:
    if not isinstance(content, list):
        return content, 0

    kept_parts: list[Any] = []
    removed = 0
    for part in content:
        if is_image_content_block(part):
            removed += 1
            continue
        kept_parts.append(part)

    if not removed:
        return content, 0
    if not kept_parts:
        return IMAGE_CONTENT_OMITTED, removed

    text_parts: list[str] = []
    for part in kept_parts:
        text = _text_from_content_part(part)
        if text is None:
            return kept_parts, removed
        if text:
            text_parts.append(text)
    return "\n".join(text_parts).strip() or IMAGE_CONTENT_OMITTED, removed


def strip_image_content_from_model_context(context: Any) -> int:
    removed_total = 0
    for message in context.get_messages():
        sanitized_content, removed = strip_image_content_blocks(
            getattr(message, "content", None)
        )
        if not removed:
            continue
        message.content = sanitized_content
        removed_total += removed
    return removed_total


def prepare_multimodal_image_context_window(
    window: Any,
    image_files: list[dict[str, Any]] | None = None,
) -> tuple[Any, int]:
    context_messages = list(getattr(window, "context_messages", []) or [])
    updated_messages, injected = prepare_multimodal_image_messages(
        context_messages,
        image_files,
    )
    if not injected:
        return window, 0

    model_copy = getattr(window, "model_copy", None)
    if callable(model_copy):
        return model_copy(update={"context_messages": updated_messages}), injected
    window.context_messages = updated_messages
    return window, injected


def ensure_multimodal_image_window_mutator(
    context: Any,
    image_files: list[dict[str, Any]] | None = None,
) -> bool:
    mutators = getattr(context, "_window_mutators", None)
    if not isinstance(mutators, list):
        return False

    images = _normalize_image_files(
        image_files if image_files is not None else current_multimodal_image_files()
    )
    mutators[:] = [
        mutator
        for mutator in mutators
        if not bool(getattr(mutator, _MULTIMODAL_IMAGE_WINDOW_MUTATOR_ATTR, False))
    ]
    if not images:
        return False

    async def multimodal_image_window_mutator(_context: Any, window: Any) -> Any:
        return prepare_multimodal_image_context_window(window, images)[0]

    setattr(
        multimodal_image_window_mutator,
        _MULTIMODAL_IMAGE_WINDOW_MUTATOR_ATTR,
        True,
    )
    mutators.append(multimodal_image_window_mutator)
    return True


def _normalize_image_files(image_files: list[dict[str, Any]] | tuple[Any, ...]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for item in image_files:
        image = _normalize_image_file(item)
        if not image:
            continue
        path = image["path"]
        if path in seen_paths:
            continue
        seen_paths.add(path)
        normalized.append(image)
    return normalized


def _normalize_image_file(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    item_type = item.get("type")
    if item_type is not None and item_type != "image":
        return None

    path = str(item.get("path") or "").strip()
    if not path:
        return None

    mime_type = str(item.get("mime_type") or item.get("mimeType") or "").strip().lower()
    if not mime_type:
        mime_type = mimetypes.guess_type(path)[0] or ""
    if mime_type not in _VISION_INLINE_IMAGE_MIME_TYPES:
        return None

    filename = str(item.get("filename") or Path(path).name).strip() or Path(path).name
    image: dict[str, Any] = {
        "type": "image",
        "filename": filename,
        "path": path,
        "mime_type": mime_type,
    }
    size_bytes = item.get("size_bytes")
    if isinstance(size_bytes, int):
        image["size_bytes"] = size_bytes
    return image


def _latest_user_message_index(messages: list[Any]) -> int:
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if not _is_user_message(message):
            continue
        content = (
            message.get("content")
            if isinstance(message, dict)
            else getattr(message, "content", None)
        )
        text = _content_to_query_text(content).lstrip()
        if text.startswith("<system-reminder>") and not _without_leading_system_reminders(text):
            continue
        return index
    return -1


def _is_user_message(message: Any) -> bool:
    role = (
        message.get("role")
        if isinstance(message, dict)
        else getattr(message, "role", None)
    )
    if str(role or "").lower() == "user":
        return True
    return message.__class__.__name__ == "UserMessage"


def _append_image_files_to_content(
    content: Any,
    image_files: list[dict[str, Any]],
) -> tuple[Any, int]:
    image_url_parts: list[Any] = []
    injected_files: list[dict[str, Any]] = []
    for image_file in image_files:
        data_uri = _image_data_uri_from_path(image_file["path"], image_file["mime_type"])
        if _append_image_url_part(image_url_parts, data_uri):
            injected_files.append(image_file)
    if not injected_files:
        return content, 0

    parts: list[Any] = [{"type": "text", "text": _build_query_file_text(content, injected_files)}]
    parts.extend(image_url_parts)
    return parts, len(injected_files)


def _build_query_file_text(content: Any, image_files: list[dict[str, Any]]) -> str:
    """Serialize the user query and attached images as a ``{query, file}`` JSON text block.

    The multimodal image content parts are appended separately; this text block gives the
    model both the original query and the image file paths in a single serialized payload.
    """

    payload = {
        "query": _content_to_query_text(content),
        "file": [
            {
                "filename": image_file["filename"],
                "path": image_file["path"],
                "mime_type": image_file["mime_type"],
            }
            for image_file in image_files
        ],
    }
    return json.dumps(payload, ensure_ascii=False)


def _content_to_query_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts: list[str] = []
        for part in content:
            text = _text_from_content_part(part)
            if text:
                texts.append(text)
        return "\n".join(texts)
    if content is None:
        return ""
    return str(content)


def _text_from_content_part(part: Any) -> str | None:
    if isinstance(part, str):
        return part
    if isinstance(part, dict) and isinstance(part.get("text"), str):
        return part["text"]
    return None


def _image_data_uri_from_path(path_text: str, mime_type: str) -> str:
    path = Path(path_text.strip())
    if not path.is_file():
        return ""
    if mime_type not in _VISION_INLINE_IMAGE_MIME_TYPES:
        return ""
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    if not data or len(data) > _MAX_VISION_INLINE_IMAGE_BYTES:
        return ""
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _append_image_url_part(parts: list[Any], data_uri: str) -> bool:
    if not data_uri.startswith("data:image/"):
        return False
    parts.append({"type": "image_url", "image_url": {"url": data_uri}})
    return True


def _copy_message_with_content(message: Any, content: Any) -> Any:
    if isinstance(message, dict):
        updated = dict(message)
        updated["content"] = content
        return updated
    model_copy = getattr(message, "model_copy", None)
    if callable(model_copy):
        return model_copy(update={"content": content})
    updated = copy.copy(message)
    updated.content = content
    return updated
