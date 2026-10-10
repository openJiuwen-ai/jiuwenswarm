# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""User prompt helpers for multimodal image attachments."""

from __future__ import annotations

import base64
import copy
import json
import mimetypes
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from stat import S_ISREG
from typing import Any, NamedTuple

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
# Max number of base64 encodings to keep
_MAX_VISION_INLINE_IMAGE_COUNT = 8
_MULTIMODAL_IMAGE_WINDOW_MUTATOR_ATTR = "_jiuwenswarm_multimodal_image_window_mutator"
_MULTIMODAL_IMAGE_WINDOW_MUTATOR_SIGNATURE_ATTR = (
    "_jiuwenswarm_multimodal_image_window_mutator_signature"
)
_CURRENT_MULTIMODAL_IMAGE_FILES: ContextVar[tuple[dict[str, Any], ...]] = ContextVar(
    "jiuwenswarm_current_multimodal_image_files",
    default=(),
)


@dataclass
class _MultimodalImageScope:
    """Per-request state for inline image injection.

    Holds the cache with the base64-encoded images so the encoding is only done once per request.
    Also, stores the mutators so they can be dropped when the request ends and not stay stale.
    """

    scope_id: str
    data_uri_cache: dict[str, tuple[int, str]] = field(default_factory=dict)
    installed_mutators: list[tuple[list[Any], Any]] = field(default_factory=list)

    def release(self) -> None:
        for mutators, mutator in self.installed_mutators:
            try:
                mutators[:] = [item for item in mutators if item is not mutator]
            except Exception:  # pragma: no cover - defensive, list is owned by Core
                logger.debug(
                    "Failed to uninstall multimodal image window mutator",
                    exc_info=True,
                )
        self.installed_mutators.clear()
        self.data_uri_cache.clear()


class MultimodalImageScopeToken(NamedTuple):
    """Opaque token returned by :func:`set_current_multimodal_image_files`."""

    files_token: Any
    scope_token: Any


_CURRENT_MULTIMODAL_IMAGE_SCOPE: ContextVar[_MultimodalImageScope | None] = ContextVar(
    "jiuwenswarm_current_multimodal_image_scope",
    default=None,
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


def set_current_multimodal_image_files(image_files: list[dict[str, Any]]) -> Any:
    """Bind request image files to the current Core model-call task."""

    files_token = _CURRENT_MULTIMODAL_IMAGE_FILES.set(
        tuple(_normalize_image_files(image_files))
    )
    scope_token = _CURRENT_MULTIMODAL_IMAGE_SCOPE.set(
        _MultimodalImageScope(scope_id=uuid.uuid4().hex)
    )
    return MultimodalImageScopeToken(files_token, scope_token)


def reset_current_multimodal_image_files(token: Any) -> None:
    """Release the request image binding, its encode cache and its window mutators."""

    if isinstance(token, MultimodalImageScopeToken):
        files_token, scope_token = token
    else:
        # Tokens minted before the scope existed (or by direct callers).
        files_token, scope_token = token, None

    if scope_token is not None:
        scope = _CURRENT_MULTIMODAL_IMAGE_SCOPE.get()
        if scope is not None:
            scope.release()
        _CURRENT_MULTIMODAL_IMAGE_SCOPE.reset(scope_token)
    if files_token is not None:
        _CURRENT_MULTIMODAL_IMAGE_FILES.reset(files_token)


def current_multimodal_image_files() -> list[dict[str, Any]]:
    return list(_CURRENT_MULTIMODAL_IMAGE_FILES.get())


def prepare_multimodal_image_messages(
    messages: list[Any],
    image_files: list[dict[str, Any]] | None = None,
    *,
    prepared: tuple[list[str], list[dict[str, Any]]] | None = None,
) -> tuple[list[Any], int]:
    """Inject inline image parts into the latest user message.

    ``prepared`` is the ``(data_uris, injected_files)`` pair returned by
    :func:`_build_image_url_parts`. Passing it reuses an encode performed earlier
    instead of re-reading and re-encoding every attached image.
    """

    if prepared is not None:
        if not prepared[1]:
            return messages, 0
        images: list[dict[str, Any]] = list(prepared[1])
    else:
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
    updated_content, injected = _append_image_files_to_content(
        content,
        images,
        prepared=prepared,
    )
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
    *,
    prepared: tuple[list[str], list[dict[str, Any]]] | None = None,
) -> tuple[Any, int]:
    context_messages = list(getattr(window, "context_messages", []) or [])
    updated_messages, injected = prepare_multimodal_image_messages(
        context_messages,
        image_files,
        prepared=prepared,
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
    """Install the inline-image context-window mutator, at most once per image set.

    Returns ``True`` only when a mutator was newly installed. Re-invocations for the
    same request and the same images leave the existing mutator (and its encoded
    payload) in place, so the images are encoded once per request rather than once per
    ReAct step.
    """

    mutators = getattr(context, "_window_mutators", None)
    if not isinstance(mutators, list):
        return False

    images = _normalize_image_files(
        image_files if image_files is not None else current_multimodal_image_files()
    )
    signature = _multimodal_image_mutator_signature(images)
    installed = [
        mutator
        for mutator in mutators
        if bool(getattr(mutator, _MULTIMODAL_IMAGE_WINDOW_MUTATOR_ATTR, False))
    ]
    if (
        images
        and len(installed) == 1
        and getattr(installed[0], _MULTIMODAL_IMAGE_WINDOW_MUTATOR_SIGNATURE_ATTR, None)
        == signature
    ):
        return False

    if installed:
        _uninstall_multimodal_image_window_mutators(mutators)
    if not images:
        return False

    prepared = _build_image_url_parts(images)
    if not prepared[1]:
        return False

    async def multimodal_image_window_mutator(_context: Any, window: Any) -> Any:
        return prepare_multimodal_image_context_window(window, prepared=prepared)[0]

    setattr(
        multimodal_image_window_mutator,
        _MULTIMODAL_IMAGE_WINDOW_MUTATOR_ATTR,
        True,
    )
    setattr(
        multimodal_image_window_mutator,
        _MULTIMODAL_IMAGE_WINDOW_MUTATOR_SIGNATURE_ATTR,
        signature,
    )
    mutators.append(multimodal_image_window_mutator)
    scope = _CURRENT_MULTIMODAL_IMAGE_SCOPE.get()
    if scope is not None:
        scope.installed_mutators.append((mutators, multimodal_image_window_mutator))
    return True


def remove_multimodal_image_window_mutator(context: Any) -> int:
    """Uninstall any inline-image window mutator from ``context``.

    Used when native image input is disabled for a call: Core shares one
    ``_window_mutators`` list across every context an agent creates, so a mutator left
    over from an earlier request would keep injecting that request's images.
    """

    mutators = getattr(context, "_window_mutators", None)
    if not isinstance(mutators, list):
        return 0
    return _uninstall_multimodal_image_window_mutators(mutators)


def _uninstall_multimodal_image_window_mutators(mutators: list[Any]) -> int:
    kept = [
        mutator
        for mutator in mutators
        if not bool(getattr(mutator, _MULTIMODAL_IMAGE_WINDOW_MUTATOR_ATTR, False))
    ]
    removed = len(mutators) - len(kept)
    if not removed:
        return 0
    mutators[:] = kept
    scope = _CURRENT_MULTIMODAL_IMAGE_SCOPE.get()
    if scope is not None:
        scope.installed_mutators[:] = [
            entry for entry in scope.installed_mutators if entry[0] is not mutators
        ]
    return removed


def _multimodal_image_mutator_signature(
    images: list[dict[str, Any]],
) -> tuple[str, tuple[tuple[str, str], ...]]:
    """Identify an installed mutator by request scope *and* image set.

    The scope id is part of the signature because Core's ``_window_mutators`` list is
    shared across every context an agent instance creates: without it, a second request
    that happened to attach the same paths would silently reuse the first request's
    encoded payload instead of rebuilding.
    """

    scope = _CURRENT_MULTIMODAL_IMAGE_SCOPE.get()
    scope_id = scope.scope_id if scope is not None else ""
    return scope_id, tuple(
        (image["path"], image["mime_type"]) for image in images
    )


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


def _build_image_url_parts(
    image_files: list[dict[str, Any]],
) -> tuple[list[str], list[dict[str, Any]]]:
    """Encode images once (via the per-request cache) and apply the inline caps.

    Returns ``(data URIs, the image records actually injected)``. Data URIs rather than
    content-part dicts: the dicts are rebuilt per call so no mutable object is aliased
    across model calls, while the large base64 strings are shared by reference.
    """

    data_uris: list[str] = []
    injected_files: list[dict[str, Any]] = []
    dropped: list[str] = []

    for image_file in image_files:
        label = image_file.get("filename") or image_file.get("path") or "<unnamed>"
        if len(injected_files) >= _MAX_VISION_INLINE_IMAGE_COUNT:
            dropped.append(f"{label} (over the {_MAX_VISION_INLINE_IMAGE_COUNT}-image cap)")
            continue
        entry, reason = _load_inline_image(image_file["path"], image_file["mime_type"])
        if entry is None:
            dropped.append(f"{label} ({reason})")
            continue
        data_uris.append(entry[1])
        injected_files.append(image_file)

    if dropped:
        logger.warning(
            "Skipped %d image attachment(s) for inline model input: %s",
            len(dropped),
            "; ".join(dropped),
        )
    return data_uris, injected_files


def _load_inline_image(
    path_text: str,
    mime_type: str,
) -> tuple[tuple[int, str] | None, str]:
    """Return ``((raw_size, data_uri), "")`` or ``(None, reason)``.

    Consults the per-request encode cache first, so a file is read and base64-encoded at
    most once per request. Size is probed with ``stat`` before reading, so an over-sized
    file is never loaded into memory.
    """

    if mime_type not in _VISION_INLINE_IMAGE_MIME_TYPES:
        return None, "unsupported mime type"

    path = Path(path_text.strip())
    cache = _current_inline_image_cache()
    cache_key = str(path)
    if cache is not None:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached, ""

    size = _regular_file_size(path)
    if size is None:
        return None, "missing or unreadable file"
    if size <= 0:
        return None, "empty file"
    if size > _MAX_VISION_INLINE_IMAGE_BYTES:
        return None, "over the per-image size cap"

    try:
        data = path.read_bytes()
    except OSError:
        return None, "missing or unreadable file"
    if not data:
        return None, "empty file"
    if len(data) > _MAX_VISION_INLINE_IMAGE_BYTES:
        # The file grew between stat() and read().
        return None, "over the per-image size cap"

    entry = (
        len(data),
        f"data:{mime_type};base64,{base64.b64encode(data).decode('ascii')}",
    )
    if cache is not None:
        cache[cache_key] = entry
    return entry, ""


def _current_inline_image_cache() -> dict[str, tuple[int, str]] | None:
    scope = _CURRENT_MULTIMODAL_IMAGE_SCOPE.get()
    return None if scope is None else scope.data_uri_cache


def _regular_file_size(path: Path) -> int | None:
    try:
        file_stat = path.stat()
    except OSError:
        return None
    if not S_ISREG(file_stat.st_mode):
        return None
    return file_stat.st_size


def _append_image_files_to_content(
    content: Any,
    image_files: list[dict[str, Any]],
    *,
    prepared: tuple[list[str], list[dict[str, Any]]] | None = None,
) -> tuple[Any, int]:
    data_uris, injected_files = (
        prepared if prepared is not None else _build_image_url_parts(image_files)
    )
    if not injected_files:
        return content, 0

    # The text block is rebuilt per call: it is derived from the *current* message
    # content, which changes on every ReAct step. Only the image data is reusable.
    parts: list[Any] = [{"type": "text", "text": _build_query_file_text(content, injected_files)}]
    parts.extend(
        {"type": "image_url", "image_url": {"url": data_uri}} for data_uri in data_uris
    )
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
