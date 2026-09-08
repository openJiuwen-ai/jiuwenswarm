"""Publish an RSI-owned copy through the existing extension catalog APIs."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

from jiuwenswarm.server.runtime import extension_package_manager as catalog


def _relative(path: str, root: Path) -> str:
    value = Path(path)
    if not value.is_absolute():
        value = root / value
    return value.resolve(strict=True).relative_to(root.resolve()).as_posix()


def _export_manifest(source: Path, destination: Path, installation_id: str) -> None:
    from openjiuwen.harness.resources import find_plugin_manifest, load_plugin_package

    manifest = Path(find_plugin_manifest(source))
    spec = load_plugin_package(manifest)
    if manifest.name == "manifest.json":
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        # Modern declarations retain display metadata and host-managed MCP refs.
        for field in ("tools", "rails", "skills", "prompt_sections", "mcps"):
            for entry in payload.get(field, []):
                if isinstance(entry, dict):
                    for key in ("file", "dir", "cwd"):
                        if entry.get(key):
                            entry[key] = _relative(entry[key], source)
        payload["skills"] = [
            {"dir": _relative(entry, source)} if isinstance(entry, str) else entry
            for entry in payload.get("skills", [])
        ]
    else:
        # Let the native loader interpret legacy sidecars; do not invent a
        # second legacy parser or drop capabilities to make a listing card.
        payload = {"package_type": "plugin", "name": spec.name or spec.id,
                   "description": spec.description or "", "metadata": spec.metadata}
        payload["prompt_sections"] = [item.model_dump() for item in spec.prompt_sections]
        for field, kind in (("tools", "tool"), ("rails", "rail")):
            payload[field] = []
            for item in getattr(spec, field):
                if item.type != f"harness.{kind}.file" or set(item.params) - {"file_path", "class_name"}:
                    raise ValueError(f"Cannot export {item.type} to a native plugin manifest")
                payload[field].append({"file": _relative(item.params["file_path"], source),
                                       "class": item.params.get("class_name")})
        payload["skills"] = [dict(item.model_dump(), dir=_relative(item.dir, source)) for item in spec.skills]
        payload["mcps"] = [item.model_dump(exclude_none=True) for item in spec.mcps]
        for item in payload["mcps"]:
            if item.get("cwd"):
                item["cwd"] = _relative(item["cwd"], source)
    payload["id"] = installation_id
    payload["version"] = installation_id.removeprefix("rsi-harness-")
    display = payload.get("display_name") or payload.get("name") or spec.id
    if not isinstance(display, dict):
        display = {"zh": str(display), "en": str(display)}
    payload["display_name"] = {
        language: f"{name} (RSI {payload['version'][:8]})" for language, name in display.items()
    }
    (destination / "manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    load_plugin_package(destination / "manifest.json")


def _contents(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }


def register_harness_plugin(source: Path, installation_id: str) -> Callable[[], None]:
    """Register/install a versioned copy; return compensation for activation failure.

    The immutable task version remains the RSI runtime and rollback authority.
    The independent catalog copy survives task removal and uses the ordinary
    plugin selection/loading path. Never overwrite an existing edited package.
    """
    source = source.resolve(strict=True)
    if not installation_id.startswith("rsi-harness-") or not installation_id.removeprefix("rsi-harness-").isalnum():
        raise ValueError("Invalid RSI installation id")
    if any(path.is_symlink() for path in source.rglob("*")):
        raise ValueError("RSI catalog packages must not contain symlinks")
    params = {"id": installation_id}
    existing = catalog.show_plugin_package(installation_id)
    was_installed = catalog.is_plugin_allowed(installation_id)
    imported = False

    def undo() -> None:
        if imported:
            catalog.uninstall_plugin_package(params)
        elif existing is not None and not was_installed:
            catalog.upsert_plugin_marketplace_entry(installation_id, installed=False, source="local")

    with tempfile.TemporaryDirectory(prefix="rsi_plugin_") as tmp:
        export = Path(tmp) / installation_id
        shutil.copytree(source, export)
        _export_manifest(source, export, installation_id)
        if existing is not None:
            if existing.get("source") != "local":
                raise ValueError(f"RSI plugin id conflicts with an existing package: {installation_id}")
            installed_path = catalog.resolve_plugin_dir(installation_id)
            if _contents(installed_path) != _contents(export):
                raise ValueError(f"RSI catalog package has been modified: {installation_id}")
        try:
            if existing is None:
                catalog.import_plugin_package({"path": str(export)})
                imported = True
            catalog.install_plugin_package(params)
        except Exception:
            undo()
            raise
    return undo
