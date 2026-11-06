# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Manifest metadata for settings-tree contributions; no plugin business state."""

from collections.abc import Mapping
from typing import Any
import re

from jiuwenswarm.extensions_v2.webui.icon_whitelist_generated import WEBUI_ICON_NAMES

SETTINGS_SLOT = "application.settings"


def _title(value: object, name: str) -> dict[str, str]:
    if (not isinstance(value, Mapping) or set(value) != {"zh", "en"}
            or any(not isinstance(item, str) or not item.strip() or len(item.encode("utf-8")) > 1024
                   for item in value.values())):
        raise ValueError(f"settings {name} requires nonempty zh/en text")
    return dict(value)


def parse_settings(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Validate declaration shape; the frontend resolves locations against its native tree."""
    if raw.get("slot") != SETTINGS_SLOT:
        raise ValueError("settings declarations require application.settings")
    kind = raw.get("kind", "item")
    if kind not in ("menu", "section", "item"):
        raise ValueError("settings kind must be menu, section or item")
    if not isinstance(raw.get("id"), str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}", raw["id"]):
        raise ValueError("invalid settings contribution id")
    location = raw.get("location")
    required = set() if kind == "menu" else {"menu"} if kind == "section" else {"menu", "section"}
    if (not isinstance(location, Mapping) or not required <= location.keys()
            or set(location) - (required | {"before", "after"})
            or {"before", "after"} <= location.keys()
            or any(not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > 256
                   for value in location.values())):
        raise ValueError("invalid settings location")
    result: dict[str, Any] = {"kind": kind, "location": dict(location)}
    if kind != "item" or "title" in raw:
        result["title"] = _title(raw.get("title"), "title")
    if "description" in raw:
        if "title" not in result:
            raise ValueError("settings description requires a title")
        result["description"] = _title(raw["description"], "description")
    if kind == "menu":
        if raw.get("icon") not in WEBUI_ICON_NAMES:
            raise ValueError("settings menus require an allowlisted icon")
        result["icon"] = raw["icon"]
    if kind == "item" and raw.get("renderer") != {"kind": "webui-tree"}:
        raise ValueError("settings items require the webui-tree renderer")
    if kind != "item" and "renderer" in raw:
        raise ValueError("settings menus and sections do not have a renderer")
    if kind == "item":
        scope = raw.get("scope", "session")
        if scope not in ("session", "application"):
            raise ValueError("invalid settings scope")
        instance_key = raw.get("instanceKey", raw["id"])
        if (not isinstance(instance_key, str) or not instance_key.strip() or instance_key != instance_key.strip()
                or "|" in instance_key or len(instance_key.encode("utf-8")) > 200):
            raise ValueError("invalid settings instanceKey")
        result.update(scope=scope, instance_key=instance_key, presentation="row" if "title" in result else "content")
    elif "scope" in raw or "instanceKey" in raw:
        raise ValueError("settings menus and sections do not have a render target")
    return result
