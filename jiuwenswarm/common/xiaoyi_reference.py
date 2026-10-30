# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Build Xiaoyi A2A data.reference parts (aligned with OpenClaw sendReference)."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit


_REQUIRED = ("title", "url", "source", "name")
_DEFAULT_APPEND_REFERENCE_CHANNELS = frozenset({"desktop", "xiaoyi"})


def resolve_append_reference_enabled(
    config: dict[str, Any] | None,
    channel_id: str | None,
) -> bool:
    """Resolve the reference-tool switch for the current channel.

    Both the direct desktop E2A path and the Xiaoyi A2A gateway path can carry
    references. ``channels.<id>.append_reference_allowed`` remains the explicit
    override; when absent, desktop and xiaoyi default on so existing config keeps
    working without a migration step.
    """
    channel = str(channel_id or "").strip()
    if not channel:
        return False
    allowed: Any = None
    if isinstance(config, dict):
        channels = config.get("channels")
        if isinstance(channels, dict):
            channel_config = channels.get(channel)
            if isinstance(channel_config, dict):
                allowed = channel_config.get("append_reference_allowed")
    if allowed is None:
        return channel in _DEFAULT_APPEND_REFERENCE_CHANNELS
    if isinstance(allowed, str):
        return allowed.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return bool(allowed)


def _safe_http_url(value: Any) -> str:
    """Return an absolute http(s) URL, otherwise an empty string."""
    url = str(value or "").strip()
    if not url:
        return ""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return ""
    scheme = parsed.scheme.lower()
    if scheme:
        if scheme not in {"http", "https"} or not parsed.netloc:
            return ""
        return url

    candidate = f"https:{url}" if url.startswith("//") else f"https://{url}"
    try:
        normalized = urlsplit(candidate)
    except ValueError:
        return ""
    hostname = normalized.hostname or ""
    if "." not in hostname or any(char.isspace() for char in url):
        return ""
    return candidate


def _from_protocol_item(row: dict[str, Any]) -> dict[str, str]:
    """Accept an already-built Huawei ReferenceDataObject for compatibility."""
    outer_params = row.get("params")
    card = row.get("card")
    card_params = card.get("params") if isinstance(card, dict) else None
    if not isinstance(outer_params, dict) or not isinstance(card_params, dict):
        return {}

    link = card_params.get("link")
    web_link = link.get("webLink") if isinstance(link, dict) else None
    image_info = card_params.get("imageInfo")
    image_small = image_info.get("small") if isinstance(image_info, dict) else None
    return {
        "title": str(card_params.get("title") or "").strip(),
        "url": _safe_http_url(
            (web_link.get("url") or "") if isinstance(web_link, dict) else ""
        ),
        "source": str(outer_params.get("source") or "").strip(),
        "name": str(
            outer_params.get("name") or card_params.get("subTitle") or ""
        ).strip(),
        "imageUrl": _safe_http_url(
            (image_small.get("url") or "") if isinstance(image_small, dict) else ""
        ),
    }


def coerce_references(raw: Any) -> list[dict[str, str]]:
    """Normalize tool input into a list of reference dicts.

    Accepts a native list, a JSON array/object string, or a single dict.
    """
    if raw is None:
        return []
    value: Any = raw
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return []
        try:
            value = json.loads(stripped)
        except (TypeError, ValueError):
            return []
    if isinstance(value, dict) and isinstance(value.get("references"), list):
        value = value["references"]
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []
    items: list[dict[str, str]] = []
    for row in value:
        if not isinstance(row, dict):
            continue
        protocol_item = _from_protocol_item(row)
        flat_item = {
            "title": str(row.get("title") or "").strip(),
            "url": _safe_http_url(row.get("url")),
            "source": str(row.get("source") or "").strip(),
            "name": str(row.get("name") or "").strip(),
        }
        item = (
            protocol_item
            if all(protocol_item.get(key) for key in _REQUIRED)
            else flat_item
        )
        image = _safe_http_url(row.get("imageUrl") or row.get("image_url"))
        if image:
            item["imageUrl"] = image
        if all(item.get(k) for k in _REQUIRED):
            if not item.get("imageUrl"):
                item.pop("imageUrl", None)
            items.append(item)
    return items


def build_a2a_reference_items(references: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Flattened refs → nested ReferenceDataObject items for the phone client."""
    out: list[dict[str, Any]] = []
    for item in references:
        card_params: dict[str, Any] = {
            "title": item["title"],
            "subTitle": item["name"],
            "link": {
                "webLink": {
                    "startMode": 0,
                    "url": item["url"],
                }
            },
        }
        image_url = item.get("imageUrl")
        if image_url:
            card_params["imageInfo"] = {"small": {"url": image_url}}
        out.append(
            {
                "params": {
                    "name": item["name"],
                    "source": item["source"],
                },
                "card": {
                    "type": "leftPictureRightText",
                    "params": card_params,
                },
            }
        )
    return out
