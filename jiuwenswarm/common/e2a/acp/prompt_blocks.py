from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import unquote, urlparse

logger = logging.getLogger(__name__)

_IMAGE_MIME_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
_EMPTY_TEXT_PLACEHOLDER = "(see attached context)"


@dataclass
class ParsedPrompt:
    """Result of parsing ACP ``session/prompt`` content blocks.

    - ``text``: prompt text with embedded resources rendered inline.
    - ``attachments``: path-based file references (``resource_link`` blocks),
      shaped for the gateway's structured ``attachments`` param.
    - ``media_items``: base64 image payloads shaped for
      ``normalize_chat_media_attachments`` input.
    """

    text: str = ""
    attachments: list[dict[str, Any]] = field(default_factory=list)
    media_items: list[dict[str, Any]] = field(default_factory=list)

    @property
    def has_content(self) -> bool:
        return bool(self.text.strip() or self.attachments or self.media_items)


def parse_prompt_blocks(
    params: dict[str, Any],
    *,
    fallback_keys: tuple[str, ...] = ("text", "content", "query"),
) -> ParsedPrompt:
    """Parse ACP content blocks from ``params["prompt"]``.

    Supported block types: ``text``, ``resource`` (embedded context),
    ``resource_link`` and ``image``. Unknown block types are ignored.
    When the prompt list yields no text, ``fallback_keys`` are consulted
    (legacy non-list prompt payloads).
    """
    parsed = ParsedPrompt()
    text_parts: list[str] = []

    prompt = params.get("prompt") if isinstance(params, dict) else None
    if isinstance(prompt, list):
        for item in prompt:
            if not isinstance(item, dict):
                continue
            block_type = str(item.get("type") or "").strip()
            if block_type == "text":
                text = item.get("text")
                if isinstance(text, str) and text:
                    text_parts.append(text)
            elif block_type == "resource":
                _parse_resource_block(item, text_parts, parsed)
            elif block_type == "resource_link":
                _parse_resource_link_block(item, text_parts, parsed)
            elif block_type == "image":
                _parse_image_block(item, parsed)
            else:
                logger.debug(
                    "[ACP] ignoring unsupported prompt block type: %s",
                    block_type or "<empty>",
                )

    if not text_parts and isinstance(params, dict):
        for key in fallback_keys:
            value = params.get(key)
            if isinstance(value, str) and value.strip():
                text_parts.append(value.strip())
                break

    parsed.text = "\n".join(text_parts)
    if not parsed.text.strip() and (parsed.attachments or parsed.media_items):
        parsed.text = _EMPTY_TEXT_PLACEHOLDER
    return parsed


def _parse_resource_block(
    item: dict[str, Any],
    text_parts: list[str],
    parsed: ParsedPrompt,
) -> None:
    resource = item.get("resource")
    if not isinstance(resource, dict):
        return
    uri = str(resource.get("uri") or "").strip()
    text = resource.get("text")
    if isinstance(text, str) and text:
        uri_attr = f' uri="{uri}"' if uri else ""
        text_parts.append(f"<context{uri_attr}>\n{text}\n</context>")
        return
    blob = resource.get("blob")
    mime_type = str(resource.get("mimeType") or "").strip().lower()
    if isinstance(blob, str) and blob.strip() and mime_type in _IMAGE_MIME_TYPES:
        media_item: dict[str, Any] = {
            "type": "image",
            "base64Data": blob,
            "mimeType": mime_type,
        }
        filename = _basename_of(uri)
        if filename:
            media_item["filename"] = filename
        parsed.media_items.append(media_item)
        return
    logger.debug("[ACP] ignoring non-text resource block: uri=%s mime=%s", uri, mime_type)


def _parse_resource_link_block(
    item: dict[str, Any],
    text_parts: list[str],
    parsed: ParsedPrompt,
) -> None:
    uri = str(item.get("uri") or "").strip()
    if not uri:
        return
    name = str(item.get("name") or "").strip()
    path = resource_uri_to_path(uri)
    if path:
        parsed.attachments.append(
            {
                "path": path,
                "type": "file",
                "filename": name or _basename_of(path) or "resource",
            }
        )
        return
    text_parts.append(f"[Linked resource] {name or 'resource'}: {uri}")


def _parse_image_block(item: dict[str, Any], parsed: ParsedPrompt) -> None:
    data = item.get("data")
    if not isinstance(data, str) or not data.strip():
        return
    media_item: dict[str, Any] = {
        "type": "image",
        "base64Data": data,
        "mimeType": str(item.get("mimeType") or "").strip().lower(),
    }
    uri = str(item.get("uri") or "").strip()
    filename = _basename_of(uri)
    if filename:
        media_item["filename"] = filename
    parsed.media_items.append(media_item)


def resource_uri_to_path(uri: str) -> str | None:
    """Map a ``file://`` URI or bare absolute path to a local filesystem path.

    Returns None for non-file URIs (http, zed, etc.).
    """
    if uri.startswith("file://"):
        parts = urlparse(uri)
        path = unquote(parts.path or "")
        if parts.netloc and parts.netloc.lower() != "localhost":
            return f"//{parts.netloc}{path}"
        if len(path) >= 3 and path[0] == "/" and path[2] == ":":
            path = path[1:]
        return path or None
    if uri.startswith("/"):
        return uri
    if len(uri) >= 3 and uri[1] == ":" and uri[2] in ("\\", "/"):
        return uri
    return None


def _basename_of(path_or_uri: str) -> str:
    text = str(path_or_uri or "").strip()
    if not text:
        return ""
    if "\\" in text:
        return PureWindowsPath(text).name
    return PurePosixPath(urlparse(text).path if "://" in text else text).name


__all__ = [
    "ParsedPrompt",
    "parse_prompt_blocks",
    "resource_uri_to_path",
]
