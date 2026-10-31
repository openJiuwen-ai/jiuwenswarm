# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""User prompt helpers for multimodal image attachments."""

from __future__ import annotations

import base64
import copy
import io
import mimetypes
import os
import re
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from PIL import Image

from jiuwenswarm.common.utils import logger

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
_MAX_VISION_INLINE_IMAGE_DIMENSION = 1536

_IMAGE_FORMAT_TO_MIME = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
    "GIF": "image/gif",
}

_IMAGE_EXT_TO_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

_JPEG_QUALITY_STEPS = (95, 85, 70, 50, 30, 10)
_WEBP_QUALITY_STEPS = (95, 85, 70, 50, 30, 10)
_SHRINK_SCALE_STEP = 0.85
_MIN_SHRINK_DIMENSION = 32

# 视觉 token 计量（OpenAI GPT-4o 风格 tile 算法）：图片在上下文窗口中的占用
# 按分辨率估算，而不是按 base64 字符长度或文件字节数。
_VISION_TILE_SIZE = 512
_VISION_BASE_TOKENS = 85
_VISION_TOKENS_PER_TILE = 170

_MULTIMODAL_IMAGE_WINDOW_MUTATOR_ATTR = "_jiuwenswarm_multimodal_image_window_mutator"
_CURRENT_MULTIMODAL_IMAGE_FILES: ContextVar[tuple[dict[str, Any], ...]] = ContextVar(
    "jiuwenswarm_current_multimodal_image_files",
    default=(),
)


def is_image_content_block(part: Any) -> bool:
    if not isinstance(part, dict):
        return False
    block_type = str(part.get("type") or "").strip().lower()
    if block_type in _IMAGE_CONTENT_TYPES:
        return True
    return "image_url" in part or "image" in part


_IMAGE_REFERENCE_LINE_RE = re.compile(
    r"^\[image #(\d+)\]\s+path=(\S+)\s+filename=(\S+)\s+mime=(\S+)(?:\s+size=(\d+))?\s*$"
)


def build_image_reference_line(image_file: dict[str, Any], index: int) -> str:
    """Format an image-file record as a plain-text reference line."""

    segments = [
        "[image #{}] path={} filename={} mime={}".format(
            index,
            image_file.get("path", ""),
            image_file.get("filename", ""),
            image_file.get("mime_type", ""),
        )
    ]
    size_bytes = image_file.get("size_bytes")
    if size_bytes is not None:
        segments.append("size={}".format(size_bytes))
    return " ".join(segments)


def parse_image_references(text: str) -> list[dict[str, Any]]:
    """Parse image reference lines out of free-form text."""

    refs: list[dict[str, Any]] = []
    for line in text.splitlines():
        match = _IMAGE_REFERENCE_LINE_RE.match(line.strip())
        if not match:
            continue
        ref: dict[str, Any] = {
            "index": int(match.group(1)),
            "path": match.group(2),
            "filename": match.group(3),
            "mime_type": match.group(4),
        }
        size_bytes = match.group(5)
        if size_bytes is not None:
            ref["size_bytes"] = int(size_bytes)
        refs.append(ref)
    return refs


def extract_multimodal_image_files(params: Any) -> list[dict[str, Any]]:
    """Return normalized image-file records from a request payload."""

    if not isinstance(params, dict):
        return []

    candidates: list[Any] = []
    raw_media_items = params.get("media_items")
    if isinstance(raw_media_items, list):
        candidates.extend(raw_media_items)

    files = params.get("files")
    if isinstance(files, dict):
        uploaded_images = files.get("uploaded_images")
        if isinstance(uploaded_images, list):
            candidates.extend(uploaded_images)

    normalized: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for item in candidates:
        image = _normalize_image_file(item)
        if not image:
            continue
        path = image["path"]
        if path in seen_paths:
            continue
        seen_paths.add(path)
        normalized.append(image)
    return normalized


def set_current_multimodal_image_files(image_files: list[dict[str, Any]]) -> Any:
    """Bind request image files to the current Core model-call task."""

    return _CURRENT_MULTIMODAL_IMAGE_FILES.set(tuple(_normalize_image_files(image_files)))


def reset_current_multimodal_image_files(token: Any) -> None:
    _CURRENT_MULTIMODAL_IMAGE_FILES.reset(token)


def current_multimodal_image_files() -> list[dict[str, Any]]:
    return list(_CURRENT_MULTIMODAL_IMAGE_FILES.get())


def is_current_multimodal_image_path(path: str) -> bool:
    """当前请求已随消息提供像素的图片路径。"""

    candidate = str(path or "").strip()
    if not candidate:
        return False
    uploaded = current_multimodal_image_files()
    if not uploaded:
        return False

    def _key(value: str) -> str:
        return os.path.normcase(os.path.realpath(os.path.expanduser(value)))

    target = _key(candidate)
    return any(_key(str(item.get("path") or "")) == target for item in uploaded)


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


def _message_content(message: Any) -> Any:
    if isinstance(message, dict):
        return message.get("content")
    return getattr(message, "content", None)


def filter_image_content_from_view(messages: list[Any]) -> tuple[list[Any], int]:
    """Return a new message list with image content blocks stripped from view copies.

    Historical messages are never modified: each affected message is replaced by a
    view copy produced via ``_copy_message_with_content``.
    """

    view_messages = list(messages)
    removed_total = 0
    for index, message in enumerate(view_messages):
        sanitized_content, removed = strip_image_content_blocks(_message_content(message))
        if not removed:
            continue
        view_messages[index] = _copy_message_with_content(message, sanitized_content)
        removed_total += removed
    return view_messages, removed_total


def strip_image_content_from_model_context(context: Any) -> int:
    """Strip image content blocks in place from persisted context messages.

    This is a pre-persistence cleanup used before checkpointing: unlike
    ``filter_image_content_from_view`` (the send-side view-level filter), it
    intentionally mutates history so base64 never reaches persisted history.
    """

    removed_total = 0
    for message in context.get_messages():
        sanitized_content, removed = strip_image_content_blocks(_message_content(message))
        if not removed:
            continue
        if isinstance(message, dict):
            message["content"] = sanitized_content
        else:
            message.content = sanitized_content
        removed_total += removed
    return removed_total


def collect_image_references_from_messages(messages: list[Any]) -> list[dict[str, Any]]:
    """Collect image reference lines from every message's text content."""

    refs: list[dict[str, Any]] = []
    for message in messages:
        text = _content_to_query_text(_message_content(message))
        if text:
            refs.extend(parse_image_references(text))
    return refs


def inject_image_content_from_references(
    messages: list[Any],
    *,
    max_total_bytes: int = 0,
    max_visual_tokens: int = 0,
    max_count: int = 0,
) -> tuple[list[Any], int]:
    """Materialize referenced images into the conversation history as ``image_url`` blocks.

    图像内容写入历史本身（就地修改传入的 message 对象），后续轮次直接复用
    这条消息里的 base64，不再重新读文件、不再重新缩小：

    - 每条 ``[image #n] path=...`` 引用行只处理一次，处理结果记录为
      ``[image #n attached]`` / ``[image #n omitted: ...]`` 状态行；
    - 超出单次请求预算时，从最老的图片开始替换成文字说明并记入历史，
      之后不会再被自动加回；
    - 历史中的原引用行与磁盘原文件都保留，需要更高清晰度时可按原文件重新出图。
    """

    states = _recorded_image_states(messages)
    pending: list[tuple[Any, dict[str, Any]]] = []
    seen_paths: set[str] = set()
    for message in messages:
        text = _content_to_query_text(_message_content(message))
        if not text:
            continue
        for ref in parse_image_references(text):
            if ref["index"] in states:
                continue
            image = _normalize_image_file(ref)
            if not image or image["path"] in seen_paths:
                continue
            seen_paths.add(image["path"])
            pending.append((message, image))

    logger.info(
        "[inject_image_content] refs_pending=%d max_total_bytes=%d max_visual_tokens=%d max_count=%d",
        len(pending),
        max_total_bytes,
        max_visual_tokens,
        max_count,
    )
    if not pending:
        return list(messages), 0

    # 先完成窗口计量，再读取像素组：pending 只带元数据字节数 + 视觉 token（仅读文件
    # 头部尺寸，不解码像素），预算裁掉的图不会白白读文件、缩小编码
    # （需求「先完成窗口计量，再读取像素组请求」）。
    existing = _collect_image_blocks(messages)
    pending_sizes = [
        (image["index"], _image_file_size_bytes(image), _image_file_visual_tokens(image))
        for _, image in pending
    ]
    dropped = _apply_image_budget(
        existing,
        pending_sizes,
        max_total_bytes=max_total_bytes,
        max_visual_tokens=max_visual_tokens,
        max_count=max_count,
    )

    attached = 0
    for message, image in pending:
        if image["index"] in dropped:
            _append_state_marker(
                message,
                _build_image_state_line(image["index"], "omitted", "request budget"),
            )
            continue
        data_uri = _image_data_uri_from_path(image["path"], image["mime_type"])
        if data_uri:
            _append_image_part(message, image["index"], data_uri)
            attached += 1
        else:
            _append_state_marker(
                message,
                _build_image_state_line(image["index"], "omitted", "unreadable"),
            )

    logger.info(
        "[inject_image_content] attached=%d dropped=%d existing=%d",
        attached,
        len(dropped),
        len(existing),
    )
    return list(messages), attached


def ensure_multimodal_image_window_mutator(
    context: Any,
    *,
    max_total_bytes: int = 0,
    max_visual_tokens: int = 0,
    max_count: int = 0,
) -> bool:
    mutators = getattr(context, "_window_mutators", None)
    if not isinstance(mutators, list):
        logger.info(
            "[ensure_multimodal_image_window_mutator] _window_mutators not a list: %r",
            mutators,
        )
        return False

    mutators[:] = [
        mutator
        for mutator in mutators
        if not bool(getattr(mutator, _MULTIMODAL_IMAGE_WINDOW_MUTATOR_ATTR, False))
    ]

    async def multimodal_image_window_mutator(_context: Any, window: Any) -> Any:
        context_messages = list(getattr(window, "context_messages", []) or [])
        # 就地写入历史：window.context_messages 与 context buffer 是同一批 message 对象。
        _view_messages, injected = inject_image_content_from_references(
            context_messages,
            max_total_bytes=max_total_bytes,
            max_visual_tokens=max_visual_tokens,
            max_count=max_count,
        )
        if not injected:
            return window
        model_copy = getattr(window, "model_copy", None)
        if callable(model_copy):
            return model_copy(update={"context_messages": context_messages})
        return window

    setattr(
        multimodal_image_window_mutator,
        _MULTIMODAL_IMAGE_WINDOW_MUTATOR_ATTR,
        True,
    )
    mutators.append(multimodal_image_window_mutator)
    return True


def ensure_multimodal_image_filter_window_mutator(context: Any) -> bool:
    mutators = getattr(context, "_window_mutators", None)
    if not isinstance(mutators, list):
        logger.info(
            "[ensure_multimodal_image_filter_window_mutator] _window_mutators not a list: %r",
            mutators,
        )
        return False

    mutators[:] = [
        mutator
        for mutator in mutators
        if not bool(getattr(mutator, _MULTIMODAL_IMAGE_WINDOW_MUTATOR_ATTR, False))
    ]

    async def multimodal_image_filter_window_mutator(_context: Any, window: Any) -> Any:
        context_messages = list(getattr(window, "context_messages", []) or [])
        view_messages, removed = filter_image_content_from_view(context_messages)
        if not removed:
            return window
        model_copy = getattr(window, "model_copy", None)
        if callable(model_copy):
            return model_copy(update={"context_messages": view_messages})
        return window

    setattr(
        multimodal_image_filter_window_mutator,
        _MULTIMODAL_IMAGE_WINDOW_MUTATOR_ATTR,
        True,
    )
    mutators.append(multimodal_image_filter_window_mutator)
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
    index = item.get("index")
    if isinstance(index, int):
        # 引用序号：注入/预算卸载按 [image #index] 写状态行，必须透传。
        image["index"] = index
    size_bytes = item.get("size_bytes")
    if isinstance(size_bytes, int):
        image["size_bytes"] = size_bytes
    return image


_IMAGE_STATE_LINE_RE = re.compile(r"^\[image #(\d+) (attached|omitted)\b[^\]]*\]$")


def _build_image_state_line(index: int, state: str, reason: str = "") -> str:
    if state == "attached":
        return f"[image #{index} attached]"
    if reason:
        return f"[image #{index} omitted: {reason}]"
    return f"[image #{index} omitted]"


def _iter_text_values(content: Any):
    if isinstance(content, str):
        yield content
        return
    if not isinstance(content, list):
        return
    for part in content:
        if isinstance(part, str):
            yield part
        elif isinstance(part, dict) and isinstance(part.get("text"), str):
            yield part["text"]


def _recorded_image_states(messages: list[Any]) -> dict[int, str]:
    """已处理的图片序号 → attached/omitted，避免重复注入或把卸载过的图加回。"""

    states: dict[int, str] = {}
    for message in messages:
        for text in _iter_text_values(_message_content(message)):
            for line in text.splitlines():
                match = _IMAGE_STATE_LINE_RE.match(line.strip())
                if match:
                    states[int(match.group(1))] = match.group(2)
    return states


def _message_parts(message: Any) -> list[Any]:
    content = _message_content(message)
    if isinstance(content, list):
        return list(content)
    if content is None:
        return []
    return [{"type": "text", "text": str(content)}]


def _set_message_content(message: Any, content: Any) -> None:
    if isinstance(message, dict):
        message["content"] = content
    else:
        message.content = content


def _append_parts(message: Any, parts: list[Any]) -> None:
    _set_message_content(message, _message_parts(message) + parts)


def _append_state_marker(message: Any, line: str) -> None:
    _append_parts(message, [{"type": "text", "text": line}])


def _append_image_part(message: Any, index: int, data_uri: str) -> None:
    _append_parts(
        message,
        [
            {"type": "text", "text": _build_image_state_line(index, "attached")},
            {"type": "image_url", "image_url": {"url": data_uri}},
        ],
    )


def _collect_image_blocks(messages: list[Any]) -> list[tuple[Any, int]]:
    blocks: list[tuple[Any, int]] = []
    for message in messages:
        content = _message_content(message)
        if not isinstance(content, list):
            continue
        for part_index, part in enumerate(content):
            if is_image_content_block(part):
                blocks.append((message, part_index))
    return blocks


def _image_block_bytes(part: Any) -> int:
    if not isinstance(part, dict):
        return 0
    image_url = part.get("image_url")
    url = image_url.get("url") if isinstance(image_url, dict) else image_url
    if not isinstance(url, str) or not url:
        return 0
    payload = url.partition("base64,")[2] if url.startswith("data:") else url
    return len(payload) * 3 // 4


def _data_uri_bytes(data_uri: str) -> int:
    payload = data_uri.partition("base64,")[2] if data_uri.startswith("data:") else data_uri
    return len(payload) * 3 // 4


def _image_file_size_bytes(image: dict[str, Any]) -> int:
    """图片元数据字节数：优先用记录里的 ``size_bytes``，否则 stat 磁盘文件。

    只读元数据、不读像素，供预算计量在读取像素组之前使用。
    """

    size = image.get("size_bytes")
    if isinstance(size, int) and size > 0:
        return size
    try:
        return Path(str(image.get("path") or "")).stat().st_size
    except OSError:
        return 0


def estimate_image_visual_tokens(width: int, height: int) -> int:
    """按分辨率估算图片的视觉 token 占用（OpenAI GPT-4o 风格 tile 算法）。

    图片在上下文窗口里的占用应按视觉 token 计，而不是 base64 字符长度或
    文件字节数：一张高分辨率图即便字节很小，其视觉 token 也很大。
    """

    if width <= 0 or height <= 0:
        return 0
    tiles_x = (width + _VISION_TILE_SIZE - 1) // _VISION_TILE_SIZE
    tiles_y = (height + _VISION_TILE_SIZE - 1) // _VISION_TILE_SIZE
    return _VISION_BASE_TOKENS + _VISION_TOKENS_PER_TILE * tiles_x * tiles_y


def _read_image_dimensions(path: str) -> tuple[int, int]:
    """轻量读取图片像素尺寸：只读头部元数据，不解码像素组。

    返回 ``(width, height)``，读取失败返回 ``(0, 0)``。
    """

    try:
        with Image.open(path) as image:
            return image.size
    except Exception:
        return (0, 0)


def _image_file_visual_tokens(image: dict[str, Any]) -> int:
    """图片的视觉 token 估算：优先用记录里的宽高，否则读文件头部尺寸。"""

    width = image.get("width")
    height = image.get("height")
    if isinstance(width, int) and isinstance(height, int):
        return estimate_image_visual_tokens(width, height)
    return estimate_image_visual_tokens(*_read_image_dimensions(str(image.get("path") or "")))


def _image_block_visual_tokens(part: Any) -> int:
    """已注入历史里的 ``image_url`` 块的视觉 token：从 data URI 头部读尺寸。"""

    if not isinstance(part, dict):
        return 0
    image_url = part.get("image_url")
    url = image_url.get("url") if isinstance(image_url, dict) else image_url
    if not isinstance(url, str) or not url.startswith("data:"):
        return 0
    payload = url.partition("base64,")[2]
    if not payload:
        return 0
    try:
        with Image.open(io.BytesIO(base64.b64decode(payload))) as image:
            width, height = image.size
    except Exception:
        return 0
    return estimate_image_visual_tokens(width, height)


def _replace_part_with_omission(message: Any, part_index: int) -> None:
    parts = _message_parts(message)
    if 0 <= part_index < len(parts):
        parts[part_index] = {"type": "text", "text": "[image omitted: request budget]"}
        _set_message_content(message, parts)


def _apply_image_budget(
    existing: list[tuple[Any, int]],
    pending_sizes: list[tuple[int, int, int]],
    *,
    max_total_bytes: int,
    max_visual_tokens: int,
    max_count: int,
) -> set[int]:
    """按「最老的图片先卸载」裁剪单次请求的图片预算，返回不注入的新图序号。

    ``pending_sizes`` 元素为 ``(index, size_bytes, visual_tokens)``：视觉 token 只从
    文件头部/元数据读取（不解码像素组），窗口计量先于像素读取。
    """

    dropped: set[int] = set()
    if max_total_bytes <= 0 and max_visual_tokens <= 0 and max_count <= 0:
        return dropped

    existing_bytes = 0
    existing_tokens = 0
    for message, part_index in existing:
        content = _message_content(message)
        if isinstance(content, list) and 0 <= part_index < len(content):
            existing_bytes += _image_block_bytes(content[part_index])
            existing_tokens += _image_block_visual_tokens(content[part_index])
    pending_bytes = sum(size for _, size, _tokens in pending_sizes)
    pending_tokens = sum(tokens for _, _size, tokens in pending_sizes)
    total_bytes = existing_bytes
    total_tokens = existing_tokens
    total_count = len(existing) + len(pending_sizes)

    def over_budget() -> bool:
        if max_count > 0 and total_count > max_count:
            return True
        if max_total_bytes > 0 and (total_bytes + pending_bytes) > max_total_bytes:
            return True
        return max_visual_tokens > 0 and (total_tokens + pending_tokens) > max_visual_tokens

    for message, part_index in existing:
        if not over_budget():
            break
        content = _message_content(message)
        if not isinstance(content, list) or part_index >= len(content):
            continue
        total_bytes -= _image_block_bytes(content[part_index])
        total_tokens -= _image_block_visual_tokens(content[part_index])
        total_count -= 1
        _replace_part_with_omission(message, part_index)

    for index, size, tokens in pending_sizes:
        if not over_budget():
            break
        dropped.add(index)
        pending_bytes -= size
        pending_tokens -= tokens
        total_count -= 1

    return dropped


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


def shrink_image(path: str, *, max_bytes: int, max_dimension: int) -> str:
    """Return a base64 ``data:`` URI for ``path``, shrinking the image when needed.

    与 read_file 图片共用 agent-core 的同一套缩小链路（``image_shrink``）：
    够小原样送出，超限才按质量从高到低逐档压缩，第一档放得下就停。
    只产出内存副本，源文件永不修改；读不到或无法解码时返回空串。
    """

    try:
        from openjiuwen.harness.tools.multimodal.image_shrink import (
            shrink_image_to_data_uri,
        )
    except ImportError:
        return _shrink_image_local(path, max_bytes=max_bytes, max_dimension=max_dimension)
    return shrink_image_to_data_uri(
        path,
        max_bytes=max_bytes,
        max_dimension=max_dimension,
    )


def _shrink_image_local(path: str, *, max_bytes: int, max_dimension: int) -> str:
    """Fallback shrink used when the installed openjiuwen lacks ``image_shrink``."""

    file_path = Path(path)
    if not file_path.is_file():
        return ""
    try:
        data = file_path.read_bytes()
    except OSError:
        return ""
    if not data:
        return ""

    try:
        with Image.open(file_path) as image:
            image.load()
            mime_type = _IMAGE_FORMAT_TO_MIME.get(image.format or "") or _IMAGE_EXT_TO_MIME.get(
                file_path.suffix.lower(), ""
            )
            if not mime_type:
                return ""

            width, height = image.size
            if len(data) <= max_bytes and max(width, height) <= max_dimension:
                return _build_data_uri(data, mime_type)

            encoded_bytes = _shrink_image_to_bytes(image, mime_type, max_bytes, max_dimension)
        if encoded_bytes:
            return _build_data_uri(encoded_bytes, mime_type)
    except Exception:
        pass
    return ""


def _build_data_uri(data: bytes, mime_type: str) -> str:
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _shrink_image_to_bytes(
    image: Any,
    mime_type: str,
    max_bytes: int,
    max_dimension: int,
) -> bytes | None:
    current = _resize_to_max_dimension(_prepare_for_resize(image), max_dimension)
    encoded = _encode_within_bytes(current, mime_type, max_bytes)
    if encoded is not None:
        return encoded

    while max(current.size) > _MIN_SHRINK_DIMENSION:
        next_dimension = int(max(current.size) * _SHRINK_SCALE_STEP)
        current = _resize_to_max_dimension(current, next_dimension)
        encoded = _encode_within_bytes(current, mime_type, max_bytes)
        if encoded is not None:
            return encoded
    return None


def _prepare_for_resize(image: Any) -> Any:
    if image.mode in ("RGB", "RGBA", "L"):
        return image
    if image.mode in ("LA", "PA") or (image.mode == "P" and "transparency" in image.info):
        return image.convert("RGBA")
    return image.convert("RGB")


def _resize_to_max_dimension(image: Any, max_dimension: int) -> Any:
    width, height = image.size
    longest = max(width, height)
    if longest <= max_dimension:
        return image
    ratio = max_dimension / longest
    size = (max(1, round(width * ratio)), max(1, round(height * ratio)))
    return image.resize(size, Image.Resampling.LANCZOS)


def _encode_within_bytes(image: Any, mime_type: str, max_bytes: int) -> bytes | None:
    if mime_type == "image/png":
        encoded = _save_image(image, format="PNG", optimize=True)
        return encoded if len(encoded) <= max_bytes else None
    if mime_type == "image/jpeg":
        rgb = image.convert("RGB")
        for quality in _JPEG_QUALITY_STEPS:
            encoded = _save_image(rgb, format="JPEG", quality=quality, optimize=True)
            if len(encoded) <= max_bytes:
                return encoded
        return None
    if mime_type == "image/webp":
        for quality in _WEBP_QUALITY_STEPS:
            encoded = _save_image(image, format="WEBP", quality=quality)
            if len(encoded) <= max_bytes:
                return encoded
        return None
    if mime_type == "image/gif":
        encoded = _save_image(image.convert("RGB"), format="GIF", optimize=True)
        return encoded if len(encoded) <= max_bytes else None
    return None


def _save_image(image: Any, *, format: str, **kwargs: Any) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format=format, **kwargs)
    return buf.getvalue()


def _image_data_uri_from_path(path_text: str, mime_type: str) -> str:
    path = Path(path_text.strip())
    if not path.is_file():
        return ""
    if mime_type not in _VISION_INLINE_IMAGE_MIME_TYPES:
        return ""
    return shrink_image(
        str(path),
        max_bytes=_MAX_VISION_INLINE_IMAGE_BYTES,
        max_dimension=_MAX_VISION_INLINE_IMAGE_DIMENSION,
    )


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
