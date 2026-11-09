# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Content-level helpers for image blocks in conversation messages.

无本地依赖的纯函数层：图片块判定、视图剥离、消息角色与文本抽取。``reference`` /
``history`` 复用这里的消息访问原语；本模块不 import 包内其它模块，也不 import PIL。
"""

from __future__ import annotations

import copy
from typing import Any

IMAGE_CONTENT_OMITTED = (
    "[Image content omitted from chat-model context. Use the original image "
    "path or a vision tool when image analysis is required.]"
)


_IMAGE_CONTENT_TYPES = frozenset({"image", "image_url", "input_image"})


def is_image_content_block(part: Any) -> bool:
    if not isinstance(part, dict):
        return False
    block_type = str(part.get("type") or "").strip().lower()
    if block_type in _IMAGE_CONTENT_TYPES:
        return True
    return "image_url" in part or "image" in part


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


def count_image_content_blocks(messages: Any) -> int:
    """统计历史里实际存在的 ``image_url`` 块数量（= 模型这次能看到的图片张数）。

    注入后的视图口径：``inject_image_content_from_references`` 的 ``attached`` 只是
    本轮**新增**张数，同一轮第二次 LLM 调用为 0；而策略段与视觉工具护栏要判断的是
    "视图里到底有没有像素"，因此按历史实际块计数。
    """

    total = 0
    for message in messages or ():
        content = message.get("content") if isinstance(message, dict) else getattr(
            message, "content", None
        )
        if not isinstance(content, list):
            continue
        total += sum(1 for part in content if is_image_content_block(part))
    return total


def _message_content(message: Any) -> Any:
    if isinstance(message, dict):
        return message.get("content")
    return getattr(message, "content", None)


def _message_role(message: Any) -> str:
    if isinstance(message, dict):
        role = message.get("role")
    else:
        role = getattr(message, "role", None)
    return str(role or "").strip().lower()


def is_user_authored_message(message: Any) -> bool:
    """Whether the message text comes from the user (reference lines are user-only).

    引用行只由上传入口写入 user 消息；tool result / assistant / web_fetch 正文里的
    同名格式行不得触发本地图片内联（否则内容注入可绕过 file_guard 外带磁盘图片）。
    """

    return _message_role(message) in {"user", "human"}


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


def _part_with_text(part: Any, text: str) -> Any:
    if isinstance(part, str):
        return text
    if isinstance(part, dict):
        return {**part, "text": text}
    return part


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
