# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Build Xiaoyi A2A data.reference parts (aligned with OpenClaw sendReference)."""

from __future__ import annotations

import json
from typing import Any


_REQUIRED = ("title", "url", "source", "name")


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
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []
    items: list[dict[str, str]] = []
    for row in value:
        if not isinstance(row, dict):
            continue
        item = {
            "title": str(row.get("title") or "").strip(),
            "url": str(row.get("url") or "").strip(),
            "source": str(row.get("source") or "").strip(),
            "name": str(row.get("name") or "").strip(),
        }
        image = str(row.get("imageUrl") or row.get("image_url") or "").strip()
        if image:
            item["imageUrl"] = image
        if all(item.get(k) for k in _REQUIRED):
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
