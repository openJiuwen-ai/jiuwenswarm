# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The app plugin an attached conversation loads.

The first attach of an app's client imports the app plugin package
JiuwenSwarm ships (``registry``) as a local plugin; a later attach refreshes a
copy an earlier release imported. The conversation is equipped with the plugin
so every turn of it loads the plugin. A conversation belongs to at most one
app: PowerPoint and WPS never share one.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from jiuwenswarm.server.client_attachments.errors import ClientAttachError, ClientAttachErrorCode
from jiuwenswarm.server.client_attachments.registry import MANAGED_BY, AppPlugin, app_plugins

logger = logging.getLogger(__name__)

_PLUGIN_LOCK = asyncio.Lock()


async def ensure_plugin(plugin: AppPlugin) -> None:
    """Import the app plugin the first time a client needs it, or refresh an earlier release's copy."""
    async with _PLUGIN_LOCK:
        try:
            await asyncio.to_thread(_install_shipped, plugin.plugin_id, plugin.package_dir)
        except (OSError, ValueError) as exc:
            raise ClientAttachError(
                ClientAttachErrorCode.PLUGIN_UNAVAILABLE, f"{plugin.plugin_id} could not be installed: {exc}",
            ) from exc


def _install_shipped(plugin_id: str, shipped: Path) -> None:
    """Import ``shipped`` as a local plugin, or refresh the copy an earlier release imported.

    Importing copies the package once; without refreshing it a release that
    changes the manifest (a rail added or renamed) would keep loading the old
    copy. Only a copy JiuwenSwarm made (``metadata.managed_by``) is refreshed:
    a user's own plugin of the same id is left alone and the attach is refused.
    The refresh swaps the directory in place and keeps the plugin installed
    throughout, so a turn of another conversation that loads the plugin
    meanwhile never finds it missing.
    """
    from jiuwenswarm.server.runtime.extension_package_manager import (
        import_plugin_package,
        install_plugin_package,
        is_plugin_allowed,
        plugin_package_exists,
        resolve_plugin_dir,
    )

    if not plugin_package_exists(plugin_id):
        import_plugin_package({"path": str(shipped)})
        install_plugin_package({"id": plugin_id})
        return
    installed = resolve_plugin_dir(plugin_id)
    if not _managed_copy(installed):
        raise ClientAttachError(
            ClientAttachErrorCode.PLUGIN_UNAVAILABLE,
            f"a plugin named {plugin_id} that JiuwenSwarm did not install already exists",
        )
    if _fingerprint(shipped) != _fingerprint(installed):
        try:
            _swap_in(installed, shipped)
        except PermissionError as exc:
            # Windows refuses to rename a directory while a file in it is open
            # (a scanner, a turn reading it). The copy in place still works;
            # a later attach refreshes it.
            logger.warning("client_attachments: %s stays at its installed version for now: %s", plugin_id, exc)
    if not is_plugin_allowed(plugin_id):
        install_plugin_package({"id": plugin_id})


def _managed_copy(package_dir: Path) -> bool:
    try:
        manifest = json.loads((package_dir / "manifest.json").read_text("utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(manifest, dict) and (manifest.get("metadata") or {}).get("managed_by") == MANAGED_BY


# Per package directory: (file sizes and mtimes, content digest). An attach hashes
# a package again only when one of its files changed.
_FINGERPRINTS: dict[Path, tuple[tuple[tuple[str, int, int], ...], str]] = {}


def _size_and_mtime(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def _fingerprint(root: Path) -> str:
    """A digest of a package directory's files (bytecode caches aside)."""
    files = sorted(
        (path.relative_to(root).as_posix(), path) for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.relative_to(root).parts
    )
    stats = tuple((name, *_size_and_mtime(path)) for name, path in files)
    cached = _FINGERPRINTS.get(root)
    if cached is not None and cached[0] == stats:
        return cached[1]
    digest = hashlib.sha256()
    for name, path in files:
        digest.update(name.encode("utf-8") + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    _FINGERPRINTS[root] = (stats, digest.hexdigest())
    return _FINGERPRINTS[root][1]


def _swap_in(installed: Path, shipped: Path) -> None:
    """Replace ``installed`` with a copy of ``shipped`` by renaming, never leaving it absent for long.

    The copy is staged beside it; the old directory is renamed away and
    removed after the new one is in place. On POSIX a reader holding one of
    its files open keeps reading it; Windows refuses the rename while a file
    in it is open (``PermissionError``), leaving the installed copy as it was.
    """
    staging = Path(tempfile.mkdtemp(prefix=f".{installed.name}.staging-", dir=installed.parent))
    staged, retired = staging / installed.name, staging / f"{installed.name}.retired"
    try:
        shutil.copytree(shipped, staged, ignore=shutil.ignore_patterns("__pycache__"))
        os.replace(installed, retired)
        try:
            os.replace(staged, installed)
        except OSError:
            os.replace(retired, installed)
            raise
    finally:
        # When the old copy could not be put back, it stays in staging rather than being lost;
        # the next attach finds no plugin installed and imports the shipped one again.
        if installed.exists() or not retired.exists():
            shutil.rmtree(staging, ignore_errors=True)
    _FINGERPRINTS.pop(installed, None)


def _app_plugins_of(equipment: dict[str, Any], attached_plugin_id: str | None) -> set[str]:
    """The app plugins a conversation already belongs to: equipped, or its client's at the last attach."""
    plugins = set(equipment.get("plugin_names") or ()) & set(app_plugins())
    if attached_plugin_id:
        plugins.add(attached_plugin_id)
    return plugins


def check_client_plugin(session_id: str, plugin_id: str, attached_plugin_id: str | None) -> None:
    """Refuse a conversation that already belongs to another app."""
    from jiuwenswarm.server.runtime.session.session_metadata import get_session_equipment

    if _app_plugins_of(get_session_equipment(session_id, cache_bust=True), attached_plugin_id) - {plugin_id}:
        raise ClientAttachError(
            ClientAttachErrorCode.CONFLICT, "this conversation belongs to a client of another application")


def equip(session_id: str, plugin_id: str, attached_plugin_id: str | None) -> None:
    """Add the plugin to the conversation, so a turn of it loads the plugin.

    This happens at attach, not at the first ``chat.send``: a conversation may
    receive another conversation's message before its user typed anything.
    It checks again what ``check_client_plugin`` checked, as the attach awaited ``ensure_plugin`` since.
    """
    from jiuwenswarm.server.runtime.session.session_metadata import (
        get_session_equipment,
        save_session_equipment,
    )

    equipment = get_session_equipment(session_id, cache_bust=True)
    if _app_plugins_of(equipment, attached_plugin_id) - {plugin_id}:
        raise ClientAttachError(
            ClientAttachErrorCode.CONFLICT, "this conversation belongs to a client of another application")
    plugin_names = list(equipment.get("plugin_names") or ())
    if plugin_id not in plugin_names:
        plugin_names.append(plugin_id)
        save_session_equipment(
            session_id, agent_template_name=equipment.get("agent_template_name", ""),
            plugin_names=plugin_names, mcp=equipment.get("mcp", ()),
        )


def check_declared_tools(plugin: AppPlugin, capabilities: dict[str, Any]) -> None:
    """A client declares only tools its app plugin defines."""
    declared = capabilities.get("supported_tools")
    if not isinstance(declared, list) or not all(isinstance(name, str) for name in declared):
        raise ClientAttachError(
            ClientAttachErrorCode.INVALID_ARGUMENT, "supported_tools must be a list of tool names")
    unknown = sorted(set(declared) - plugin.tool_names)
    if unknown:
        raise ClientAttachError(
            ClientAttachErrorCode.INVALID_ARGUMENT,
            f"supported_tools names tools the {plugin.plugin_id} plugin does not have: {', '.join(unknown)}",
        )
