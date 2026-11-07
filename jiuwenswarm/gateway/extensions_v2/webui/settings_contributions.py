# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Settings contributions of Gateway packages, declared as AgentServer S_18 defines them.

Gateway packages contribute to the same settings tree as AgentServer packages: manifest
``ui.items`` with ``slot=application.settings``, ``kind`` menu/section/item and ``location``.
The rules mirror ``jiuwenswarm/extensions_v2/webui/settings.py`` (Gateway code may not import it;
both sides run the shared cases in ``tests/fixtures/settings_contribution_cases.json``). A Gateway
item must declare ``scope: "application"``: the Gateway addresses browsers, not chat sessions.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

from jiuwenswarm.gateway.extensions_v2.webui.icon_whitelist_generated import WEBUI_ICON_NAMES
from jiuwenswarm.gateway.extensions_v2.webui.models import valid_instance_key

SETTINGS_SLOT = "application.settings"
_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}")


def _text(value: object, name: str) -> dict[str, str]:
    if (not isinstance(value, Mapping) or set(value) != {"zh", "en"}
            or any(not isinstance(item, str) or not item.strip() or len(item.encode("utf-8")) > 1024
                   for item in value.values())):
        raise ValueError(f"settings {name} requires nonempty zh/en text")
    return dict(value)


def parse_settings(raw: Mapping[str, Any], *, gateway: bool = True) -> dict[str, Any]:
    """Validate one declaration and return its public catalog metadata (S_18 section 2)."""
    if raw.get("slot") != SETTINGS_SLOT:
        raise ValueError("settings declarations require application.settings")
    kind = raw.get("kind", "item")
    if kind not in ("menu", "section", "item"):
        raise ValueError("settings kind must be menu, section or item")
    if not isinstance(raw.get("id"), str) or not _ID.fullmatch(raw["id"]):
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
        result["title"] = _text(raw.get("title"), "title")
    if "description" in raw:
        if "title" not in result:
            raise ValueError("settings description requires a title")
        result["description"] = _text(raw["description"], "description")
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
        if gateway and scope != "application":
            raise ValueError("Gateway settings items must declare the application scope")
        instance_key = raw.get("instanceKey", raw["id"])
        if not valid_instance_key(instance_key, required=True):
            raise ValueError("invalid settings instanceKey")
        result.update(scope=scope, instance_key=instance_key, presentation="row" if "title" in result else "content")
    elif "scope" in raw or "instanceKey" in raw:
        raise ValueError("settings menus and sections do not have a render target")
    return result


def package_settings(ui: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """``(id, metadata)`` of a package's settings declarations; raises on the first invalid one."""
    items = ui.get("items", []) if isinstance(ui, Mapping) else []
    declared: list[tuple[str, dict[str, Any]]] = []
    ids: set[str] = set()
    targets: set[str] = set()
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, Mapping):
            continue
        if item.get("slot") != SETTINGS_SLOT:
            if "location" in item:
                raise ValueError("location is only supported by application.settings")
            continue
        settings = parse_settings(item)
        if item["id"] in ids:
            raise ValueError(f"duplicate settings contribution id: {item['id']}")
        ids.add(item["id"])
        if settings["kind"] == "item":
            if settings["instance_key"] in targets:
                raise ValueError(f"duplicate settings render target: {settings['instance_key']}")
            targets.add(settings["instance_key"])
        declared.append((item["id"], settings))
    return declared


def merge_catalog(agent_catalog: Mapping[str, Any], gateway_items: list[dict[str, Any]]) -> dict[str, Any]:
    """Append Gateway contributions to the AgentServer ``webui.settings.catalog`` answer.

    Each host lists its own packages; the revision covers both so the browser refreshes when either
    side changes. Without Gateway items the AgentServer answer is returned unchanged.
    """
    if not gateway_items:
        return dict(agent_catalog)
    items = [*(agent_catalog.get("items") or []), *gateway_items]
    payload = {"items": items, "errors": dict(agent_catalog.get("errors") or {})}
    revision = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return {"revision": revision, **payload}
