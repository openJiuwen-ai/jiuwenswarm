# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""User prompt helpers for multimodal image attachments."""

from __future__ import annotations

import base64
import copy
import json
import mimetypes
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


def is_image_content_block(part: Any) -> bool:
    if not isinstance(part, dict):
        return False
    block_type = str(part.get("type") or "").strip().lower()
    if block_type in _IMAGE_CONTENT_TYPES:
        return True
    return "image_url" in part or "image" in part


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


def image_input_status_message(status: str, model_label: str = "") -> str:
    """Describe the same image-input capability in prompts and fallback notices."""
    model = f"当前模型{model_label}"
    messages = {
        "disabled": f"{model}已在配置中关闭原生图片输入。",
        "unsupported": f"{model}的当前接口经检测不支持原生图片输入。",
    }
    return messages.get(status, f"尚未确认{model}的接口是否支持原生图片输入。")


def _image_tool_hint(image_input_status: str, vision_tool_available: bool, steer_image_tool: str | None) -> str:
    if steer_image_tool:
        invocation = (
            "read_file(file_path=mediaPath)"
            if steer_image_tool == "read_file"
            else "visual_question_answering(image_path_or_url=mediaPath, question=question)"
        )
        return (
            "This is supplemental input for the current task. The steering queue carries text only; "
            "the uploaded images have not yet been read. For every entry in mediaItems, call "
            f"{invocation} using that entry's mediaPath before describing or comparing the images. "
            "If reading fails or returns only metadata, explain the failure; do not guess image contents."
        )
    if vision_tool_available:
        tool_hint = (
            "请调用已配置的图片理解工具；"
            "优先使用 image_reading(local_url=mediaPath, prompt=question)，"
            "或使用 visual_question_answering(image_path_or_url=mediaPath, question=question)。"
        )
    else:
        tool_hint = (
            "没有配置可用的视觉模型工具。"
            "请根据上述状态向用户说明本次无法直接读图的原因，不要猜测图片内容。"
            "能力未确认不等于模型不支持读图。"
        )
    return image_input_status_message(image_input_status) + "本次图片未作为原生图片输入发送给主模型。" + tool_hint


def prepare_image_tool_inputs(
    params: Any,
    inputs: dict[str, Any],
    *,
    enable_read_image_multimodal: bool,
    vision_tool_available: bool,
    image_input_status: str = "unknown",
    steer_image_tool: str | None = None,
) -> dict[str, Any]:
    """Serialize image-tool context for both ordinary chat and text-only steer."""
    if enable_read_image_multimodal and steer_image_tool is None:
        return inputs
    query = inputs.get("query")
    if not isinstance(query, str) or (
        steer_image_tool is None and "jiuwenswarm_image_tool_context" in query
    ):
        return inputs
    image_files = inputs.get("_multimodal_image_files")
    if not isinstance(image_files, list) or not image_files:
        image_files = extract_multimodal_image_files(params)
    if not image_files:
        return inputs

    params = params if isinstance(params, dict) else {}
    raw_question = params.get("query")
    if not isinstance(raw_question, str) or not raw_question.strip():
        raw_question = params.get("content")
    question = raw_question.strip() if isinstance(raw_question, str) else ""
    if steer_image_tool and not question:
        question = "Read the attached images and use them to continue the current task."
    first_path = str(image_files[0].get("path") or "").strip()
    if not first_path:
        return inputs
    media_items = []
    for image_file in image_files:
        path = str(image_file.get("path") or "").strip()
        if not path:
            continue
        media_items.append({
            "type": "image",
            "filename": image_file.get("filename") or Path(path).name,
            "mediaPath": path,
            "mimeType": image_file.get("mime_type") or image_file.get("mimeType"),
        })
    if not media_items:
        return inputs
    tool_context = {
        "marker": "jiuwenswarm_image_tool_context",
        "mediaPath": first_path,
        "mediaItems": media_items,
        "question": question,
        "imageInputStatus": image_input_status,
        "toolHint": _image_tool_hint(image_input_status, vision_tool_available, steer_image_tool),
    }
    updated = dict(inputs)
    updated.pop("_multimodal_image_files", None)
    updated["query"] = (
        query
        + "\n\n图片附件上下文（供 ReAct 选择图片理解工具使用）：\n"
        + json.dumps(tool_context, ensure_ascii=False)
    )
    return updated


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
    images = _normalize_image_files(image_files or current_multimodal_image_files())
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
        if _is_user_message(messages[index]):
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
