# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Validation helpers for read_file-style tools on non-text / binary inputs."""

from __future__ import annotations

import json
import os
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from openjiuwen.core.single_agent.rail.base import AgentCallbackContext

READ_FILE_TOOL_NAMES = frozenset(
    {
        "read_file",
        "read",
        "Read",
        "read_text_file",
        "file_read",
        "memory_get",
        "read_memory",
    }
)

READ_FILE_ERROR_PREFIX = "[READ_FILE_ERROR]"

_IMAGE_EXTENSIONS = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".bmp",
        ".webp",
        ".tiff",
        ".tif",
        ".svg",
        ".ico",
        ".heic",
        ".heif",
        ".avif",
    }
)

_NON_TEXT_EXTENSIONS = frozenset(
    {
        ".zip",
        ".gz",
        ".bz2",
        ".xz",
        ".tar",
        ".7z",
        ".rar",
        ".mp3",
        ".mp4",
        ".mov",
        ".avi",
        ".mkv",
        ".wav",
        ".flac",
        ".ogg",
        ".aac",
        ".m4a",
        ".webm",
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".bin",
        ".dat",
        ".db",
        ".sqlite",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
    }
)

_BINARY_RESULT_MARKERS = (
    "binary file",
    "二进制文件",
    "cannot read binary",
    "not a text file",
    "非文本",
    "unicode decode error",
    "unicodedecodeerror",
    "invalid utf-8",
    "invalid utf8",
    "cannot decode",
    "unsupported file type",
)

_PATH_KEYS = ("path", "file_path", "file", "target_file")


def is_read_file_tool(tool_name: str) -> bool:
    return str(tool_name or "").strip() in READ_FILE_TOOL_NAMES


def _coerce_arguments_dict(arguments: Any) -> dict[str, Any] | None:
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        text = arguments.strip()
        if text.startswith("{"):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return None
            if isinstance(parsed, dict):
                return parsed
    return None


def extract_path_from_arguments(arguments: Any) -> str | None:
    """Extract file path from tool arguments (dict or JSON string)."""
    data = _coerce_arguments_dict(arguments)
    if data is not None:
        for key in _PATH_KEYS:
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None
    if isinstance(arguments, str) and arguments.strip():
        text = arguments.strip()
        if not text.startswith("{"):
            return text
    return None


def is_image_file_path(path: str) -> bool:
    if not path:
        return False
    return os.path.splitext(path)[1].lower() in _IMAGE_EXTENSIONS


def is_non_text_file_path(path: str) -> bool:
    if not path:
        return False
    ext = os.path.splitext(path)[1].lower()
    if ext in _IMAGE_EXTENSIONS or ext in _NON_TEXT_EXTENSIONS:
        return True
    return False


def build_non_text_read_error(path: str, *, language: str = "cn") -> str:
    target = path or "<unknown>"
    if language == "cn":
        hint = (
            "若要理解图片内容，请改用 visual_question_answering 工具并传入文件路径；"
            if is_image_file_path(path)
            else "若要处理 PDF/音视频等，请使用对应的专用工具。"
        )
        return (
            f"{READ_FILE_ERROR_PREFIX} 无法以文本方式读取「{target}」。"
            f"该文件是{'图片' if is_image_file_path(path) else '二进制'}文件，read_file 只能读取纯文本。"
            f"{hint}"
        )
    return (
        f"{READ_FILE_ERROR_PREFIX} Cannot read '{target}' as text. "
        "This path looks like an image or binary file. "
        "Use visual_question_answering for images, or an appropriate specialized tool "
        "for other binary formats."
    )


def is_read_file_error_message(message: str) -> bool:
    text = str(message or "").lstrip()
    return text.startswith(READ_FILE_ERROR_PREFIX)


def _extract_text_content(result: Any) -> str:
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        for key in ("content", "result", "data", "text", "output", "message", "error"):
            value = result.get(key)
            if isinstance(value, str):
                return value
        if result.get("success") is False:
            err = result.get("error")
            if isinstance(err, str):
                return err
        return str(result)
    return str(result)


def _looks_like_binary_payload(text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    if any(marker in lowered for marker in _BINARY_RESULT_MARKERS):
        return True
    if "\x00" in text:
        return True
    sample = text[:4096]
    if not sample:
        return False
    suspicious = sum(
        1 for ch in sample if ord(ch) < 9 or ord(ch) == 11 or ord(ch) == 12 or 14 <= ord(ch) <= 31
    )
    if suspicious / max(len(sample), 1) > 0.05:
        return True
    return False


def validate_read_file_result(path: str, result: Any) -> tuple[bool, str | None]:
    """Return (ok, error_message). error_message is set when read should be treated as failed."""
    if isinstance(result, dict) and result.get("success") is False:
        err = _extract_text_content(result)
        return False, err or build_non_text_read_error(path)

    text = _extract_text_content(result)
    if not text.strip():
        if is_non_text_file_path(path):
            return False, build_non_text_read_error(path)
        return False, (
            f"{READ_FILE_ERROR_PREFIX} 文件读取结果为空。"
            "请确认路径是否正确，或该文件是否为图片/二进制文件。"
        )

    if is_non_text_file_path(path) and _looks_like_binary_payload(text):
        return False, build_non_text_read_error(path)

    if _looks_like_binary_payload(text):
        return False, (
            f"{READ_FILE_ERROR_PREFIX} 读取到的内容不像有效文本（可能是二进制文件被误读）。"
            "请确认文件类型，并对图片使用 visual_question_answering。"
        )

    return True, None


def resolve_language_from_context(ctx: "AgentCallbackContext | None") -> str:
    if ctx is None:
        return "cn"
    inputs = getattr(ctx, "inputs", None)
    for attr in ("language", "preferred_language"):
        value = getattr(inputs, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    extra = getattr(ctx, "extra", None)
    if isinstance(extra, dict):
        for key in ("language", "preferred_language"):
            value = extra.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip().lower()
    return "cn"


def _write_read_file_tool_result(ctx: "AgentCallbackContext", message: str) -> None:
    from openjiuwen.core.foundation.llm import ToolMessage
    from openjiuwen.core.single_agent.rail.base import ToolCallInputs

    if not isinstance(ctx.inputs, ToolCallInputs):
        return
    tool_call = ctx.inputs.tool_call
    tool_call_id = getattr(tool_call, "id", "") if tool_call else ""
    text = str(message or "")
    ctx.inputs.tool_result = text
    tool_msg = getattr(ctx.inputs, "tool_msg", None)
    if tool_msg is not None:
        tool_msg.content = text
        if tool_call_id and getattr(tool_msg, "tool_call_id", None) in (None, ""):
            tool_msg.tool_call_id = tool_call_id
    else:
        ctx.inputs.tool_msg = ToolMessage(content=text, tool_call_id=tool_call_id)


def reject_read_file(ctx: "AgentCallbackContext", message: str) -> None:
    """Skip tool execution and inject an error result for the model/UI."""
    ctx.extra["_skip_tool"] = True
    _write_read_file_tool_result(ctx, message)


def handle_read_file_before_tool_call(ctx: "AgentCallbackContext", path: str) -> None:
    """Intercept read_file on non-text paths before execution."""
    if is_non_text_file_path(path):
        language = resolve_language_from_context(ctx)
        reject_read_file(ctx, build_non_text_read_error(path, language=language))


def normalize_read_file_tool_outcome(ctx: "AgentCallbackContext") -> None:
    """Rewrite invalid read_file results with a clear error message."""
    from openjiuwen.core.single_agent.rail.base import ToolCallInputs

    if not isinstance(ctx.inputs, ToolCallInputs):
        return
    tool_name = str(getattr(ctx.inputs, "tool_name", "") or getattr(ctx.inputs.tool_call, "name", "") or "")
    if not is_read_file_tool(tool_name):
        return

    arguments = getattr(ctx.inputs.tool_call, "arguments", {}) if ctx.inputs.tool_call else {}
    path = extract_path_from_arguments(arguments) or ""
    result = getattr(ctx.inputs, "tool_result", None)

    if is_read_file_error_message(str(result or "")):
        return

    ok, error_message = validate_read_file_result(path, result)
    if ok or not error_message:
        return

    _write_read_file_tool_result(ctx, error_message)
