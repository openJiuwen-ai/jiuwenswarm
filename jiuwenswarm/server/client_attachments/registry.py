# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The app plugins a client may attach with, found in the packages JiuwenSwarm ships (``APP_PACKAGES``).

Each entry names a directory inside an installed Python package, as
``<package>/<relative dir>``, so it resolves the same from source and from a
frozen bundle. Every subdirectory of it holding a ``manifest.json`` is an app
plugin package: its plugin id is the manifest's ``id``, and the tools its
client may declare are the ``name``s in its ``tools.json``. Its manifest
carries ``metadata.managed_by: "client_attachments"``, which marks the copy an
attach installs as JiuwenSwarm's own, so a later attach may refresh it.
"""

from __future__ import annotations

import importlib.util
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jiuwenswarm.server.client_attachments.errors import ClientAttachError, ClientAttachErrorCode

APP_PACKAGES: tuple[str, ...] = ()

MANAGED_BY = "client_attachments"


@dataclass(frozen=True)
class AppPlugin:
    plugin_id: str
    package_dir: Path
    tool_names: frozenset[str]


def app_plugins() -> dict[str, AppPlugin]:
    """Every app plugin of the shipped packages, by plugin id."""
    plugins: dict[str, AppPlugin] = {}
    for entry in APP_PACKAGES:
        module, _, relative = entry.partition("/")
        root = Path(importlib.util.find_spec(module).submodule_search_locations[0]) / relative
        for package_dir in sorted(path for path in root.iterdir() if (path / "manifest.json").is_file()):
            manifest = _read_json(package_dir / "manifest.json")
            if (manifest.get("metadata") or {}).get("managed_by") != MANAGED_BY:
                raise RuntimeError(f"{package_dir}/manifest.json needs metadata.managed_by: {MANAGED_BY!r}")
            plugin_id = manifest["id"]
            tool_names = frozenset(tool["name"] for tool in _read_json(package_dir / "tools.json"))
            plugins[plugin_id] = AppPlugin(plugin_id=plugin_id, package_dir=package_dir, tool_names=tool_names)
    return plugins


def app_plugin(plugin_id: str) -> AppPlugin:
    plugin = app_plugins().get(plugin_id)
    if plugin is None:
        raise ClientAttachError(
            ClientAttachErrorCode.PLUGIN_UNAVAILABLE, f"no client attach plugin {plugin_id} is registered")
    return plugin


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text("utf-8"))
