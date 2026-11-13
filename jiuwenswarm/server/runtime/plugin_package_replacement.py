"""Confirmed replacement of a local plugin by its same-id Hub package."""

from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from jiuwenswarm.server.runtime.marketplace.hub_asset_port import HubAssetQuery, create_default_hub_asset_port
from jiuwenswarm.server.runtime.marketplace.hub_install_state import HubInstallStateStore
from jiuwenswarm.server.runtime.plugin_package_lifecycle import (
    PluginPackageBusy,
    package_fingerprint,
    replace_plugin,
)


def _restore_file(path: Path, content: bytes | None) -> None:
    if content is None:
        path.unlink(missing_ok=True)
        return
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(content)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


async def handle_local_conflict(
    params: dict, *, hub_port: Any = None, downloader: Any = None, before_replace: Any = None,
) -> tuple[bool, dict] | None:
    """Return None for ordinary installs, or an explicit conflict/replacement result."""
    from jiuwenswarm.server.runtime import extension_package_manager as catalog

    kind = "plugin_packages"
    asset_id = catalog.lifecycle_package_id(params, "plugin")
    store = catalog.hub_install_state_store(kind)
    if store.get(asset_id) or store.get_by_package_id(asset_id):
        return None
    root = catalog.get_local_root(kind)
    if not root.is_dir() or not any(root.iterdir()):
        return None
    # A local-id request is an ordinary install of an imported package.
    if (root / asset_id).is_dir() and not params.get("replace_local") and not params.get("from_hub"):
        return None
    port = hub_port or create_default_hub_asset_port()
    detail = await port.query_asset(HubAssetQuery(kind="plugin", asset_id=asset_id))
    if detail.kind != "plugin" or detail.asset_id != asset_id:
        raise ValueError("Hub plugin identity mismatch")
    package_id = catalog.reject_package_name(detail.package_name, "plugin")
    destination = root / package_id
    manifest = catalog.read_package_manifest(destination)
    if not destination.is_dir() or manifest is None or manifest.get("id") != package_id:
        return None
    if store.get_by_package_id(package_id) is not None:
        return None
    catalog.assert_no_symlinks(destination)
    fingerprint = package_fingerprint(destination)
    conflict = {
        "package_id": package_id, "local_version": str(manifest.get("version") or ""),
        "hub_version": detail.version, "local_fingerprint": fingerprint,
    }
    if params.get("replace_local") is not True or params.get("local_fingerprint") != fingerprint:
        return False, {
            "error": "A local plugin already exists; confirm replacement",
            "error_code": "PLUGIN_LOCAL_CONFLICT", "conflict": conflict,
        }

    # Download and validate first. No live files or install records change here.
    with tempfile.TemporaryDirectory(prefix=".plugin-replacement-", dir=root) as temporary:
        staging = Path(temporary)
        prepared_id = await catalog.prepare_hub_equipment(
            kind, asset_id, hub_port=port, downloader=downloader, staging_root=staging,
        )
        if prepared_id != package_id:
            raise ValueError("Hub plugin identity changed during replacement")
        staged = staging / package_id
        staged_manifest = catalog.read_package_manifest(staged) or {}
        connectors = [item["connector"] for item in staged_manifest.get("mcps", [])
                      if isinstance(item, dict) and isinstance(item.get("connector"), str)]
        pending = catalog.unready_connectors(connectors)
        if pending:
            return False, {"error": "Plugin connectors are not ready", "pending_connectors": pending}
        record = HubInstallStateStore(staging).get(asset_id)
        if record is None:
            raise ValueError("Staged plugin is missing Hub provenance")
        try:
            with replace_plugin(package_id):
                if (store.get_by_package_id(package_id) is not None
                        or not destination.is_dir()
                        or package_fingerprint(destination) != fingerprint):
                    return False, {"error": "Local plugin changed; confirm again",
                                   "error_code": "PLUGIN_REPLACEMENT_STALE"}
                marketplace = catalog.marketplace_path(kind)
                snapshots = {path: path.read_bytes() if path.is_file() else None
                             for path in (store.path, marketplace)}
                # Keep recovery data outside TemporaryDirectory: a failed restore
                # must never cause automatic cleanup to delete the only old copy.
                backup_parent = Path(tempfile.mkdtemp(prefix=".plugin-previous-", dir=root))
                backup = backup_parent / package_id
                try:
                    async with AsyncExitStack() as live_rollback:
                        if before_replace is not None:
                            restore = await before_replace(package_id)
                            if restore is not None:
                                live_rollback.push_async_callback(restore)
                        # Detach may await shutdown; restore live selections on stale edits too.
                        if package_fingerprint(destination) != fingerprint:
                            return False, {"error": "Local plugin changed; confirm again",
                                           "error_code": "PLUGIN_REPLACEMENT_STALE"}
                        destination.replace(backup)
                        try:
                            staged.replace(destination)
                            ok, payload = catalog.install_equipment_gated(kind, {"id": package_id})
                            if not ok:
                                raise RuntimeError("Plugin dependencies changed during replacement")
                            store.upsert(record)
                            catalog.upsert_plugin_marketplace_entry(package_id, installed=True, source="hub")
                        except BaseException:
                            if destination.exists():
                                shutil.rmtree(destination)
                            backup.replace(destination)
                            for path, content in snapshots.items():
                                _restore_file(path, content)
                            raise
                        live_rollback.pop_all()
                    shutil.rmtree(backup)
                finally:
                    if not backup.exists():
                        backup_parent.rmdir()
                return True, {"id": package_id, "replaced": True}
        except PluginPackageBusy:
            return False, {"error": "Plugin is executing or closing; retry when idle",
                           "error_code": "PLUGIN_REPLACEMENT_BUSY"}
