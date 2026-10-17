#!/usr/bin/env python3
"""Register a local plugin package in marketplace.json.

Reads manifest from plugins/plugin_packages/local/<plugin-name>/ and upserts
an entry into plugins/plugin_packages/marketplace.json.

Usage:
    register_plugin.py <plugin-name>                  # create mode, no bump
    register_plugin.py <path-to-package-dir> --bump     # update mode, bump patch

Exit code: 0 success, 1 failure.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any


def write_stdout(text: str) -> None:
    """CLI product output to fd 1 (avoid print/sys.stdout for G.LOG.02)."""
    os.write(1, text.encode("utf-8"))

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")


def get_jiuwenswarm_data_dir() -> Path:
    raw = os.environ.get("JIUWENSWARM_DATA_DIR", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".jiuwenswarm"


def get_agent_workspace_dir() -> Path:
    return get_jiuwenswarm_data_dir() / "agent" / "workspace"


def get_plugin_packages_local_dir() -> Path:
    return get_agent_workspace_dir() / "plugins" / "plugin_packages" / "local"


def _infer_marketplace_path(pkg_dir: Path) -> Path | None:
    parts = pkg_dir.resolve().parts
    try:
        local_idx = parts.index("local")
    except ValueError:
        return None
    if local_idx < 1 or parts[local_idx - 1] != "plugin_packages":
        return None
    return Path(*parts[:local_idx]) / "marketplace.json"


def get_marketplace_path(pkg_dir: Path | None = None) -> Path:
    if pkg_dir is not None:
        inferred = _infer_marketplace_path(pkg_dir)
        if inferred is not None:
            return inferred
    return get_agent_workspace_dir() / "plugins" / "plugin_packages" / "marketplace.json"


def resolve_pkg(arg: str) -> Path:
    p = Path(arg).expanduser()
    if p.is_dir():
        return p.resolve()
    local = get_plugin_packages_local_dir() / arg
    if local.is_dir():
        return local.resolve()
    raise FileNotFoundError(
        f"找不到包目录: {arg}（也不在 {get_plugin_packages_local_dir()} 下）"
    )


def _bump_patch(version: str) -> str:
    parts = version.split(".")
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        return f"{parts[0]}.{parts[1]}.{int(parts[2]) + 1}"
    return f"{version}+1"


def _maybe_bump_version(
    pkg_dir: Path, manifest: dict[str, Any], *, bump: bool
) -> str:
    raw = manifest.get("version")
    cur = raw.strip() if isinstance(raw, str) and raw.strip() else "1.0.0"
    if not bump:
        return cur
    new = _bump_patch(cur)
    manifest["version"] = new
    (pkg_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return new


def _entry_from_manifest(manifest: dict[str, Any], package_id: str) -> dict[str, Any]:
    manifest_id = str(manifest.get("id") or "").strip()
    if manifest_id != package_id:
        raise ValueError(
            f"manifest.json: id ({manifest_id!r}) 必须等于包目录名 ({package_id!r})"
        )
    return {
        "id": package_id,
        "source": "local",
        "installed": True,
    }


def _slim_marketplace_entry(entry: dict[str, Any]) -> dict[str, Any]:
    slim: dict[str, Any] = {"id": entry.get("id")}
    source = entry.get("source")
    if isinstance(source, str) and source:
        slim["source"] = source
    slim["installed"] = bool(entry.get("installed", False))
    return slim


def _read_marketplace_plugins(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"marketplace.json 解析失败: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("marketplace.json 顶层必须是 JSON 对象")
    plugins = data.get("plugins")
    if plugins is None:
        return []
    if not isinstance(plugins, list):
        raise ValueError("marketplace.json: plugins 必须是数组")
    return [entry for entry in plugins if isinstance(entry, dict)]


def _upsert_plugin_entry(
    plugins: list[dict[str, Any]], entry: dict[str, Any]
) -> None:
    package_id = entry["id"]
    for index, existing in enumerate(plugins):
        if existing.get("id") != package_id:
            continue
        merged = _slim_marketplace_entry(entry)
        if existing.get("installed") is True:
            merged["installed"] = True
        plugins[index] = merged
        return
    plugins.append(_slim_marketplace_entry(entry))


def register_package(pkg_dir: Path, *, bump: bool = False) -> dict[str, Any]:
    package_id = pkg_dir.name
    if len(package_id) < 2 or not NAME_RE.match(package_id):
        raise ValueError(f"包目录名 {package_id!r} 必须是 kebab-case")

    manifest_path = pkg_dir / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"缺少 manifest.json: {manifest_path}")

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"manifest.json 解析失败: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ValueError("manifest.json 顶层必须是 JSON 对象")
    if manifest.get("package_type") != "plugin":
        raise ValueError(
            f"package_type 必须是 plugin，当前是 {manifest.get('package_type')!r}"
        )

    effective_version = _maybe_bump_version(pkg_dir, manifest, bump=bump)
    if "[TODO" in json.dumps(manifest, ensure_ascii=False):
        raise ValueError(
            "manifest 展示字段仍含 [TODO]；请先完成填充并通过 validate_plugin.py"
        )
    entry = _entry_from_manifest(manifest, package_id)
    marketplace_path = get_marketplace_path(pkg_dir)
    plugins = [
        _slim_marketplace_entry(item)
        for item in _read_marketplace_plugins(marketplace_path)
    ]
    _upsert_plugin_entry(plugins, entry)
    marketplace_path.parent.mkdir(parents=True, exist_ok=True)
    marketplace_path.write_text(
        json.dumps({"plugins": plugins}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    final = dict(next(item for item in plugins if item.get("id") == package_id))
    final["_version"] = effective_version
    return final


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Register a local plugin package in marketplace.json."
    )
    parser.add_argument(
        "target",
        help="包目录绝对路径，或 local/ 下的 plugin-name",
    )
    parser.add_argument(
        "--bump",
        action="store_true",
        help="update 模式：递增 manifest version patch 段后写回，再注册。",
    )
    args = parser.parse_args()

    try:
        pkg_dir = resolve_pkg(args.target)
        entry = register_package(pkg_dir, bump=args.bump)
    except (FileNotFoundError, ValueError) as exc:
        write_stdout(f"Error: {exc}\n")
        return 1

    marketplace_path = get_marketplace_path(pkg_dir)
    write_stdout(f"Registered plugin: {entry['id']}\n")
    write_stdout(f"  Package:     {pkg_dir}\n")
    write_stdout(f"  Marketplace: {marketplace_path}\n")
    write_stdout(f"  Version:     {entry.pop('_version', '')}\n")
    write_stdout(f"  installed:   {entry['installed']}  source: {entry['source']}\n")
    write_stdout(
        "\nNEXT:   已登记为可使用；新开对话并勾选该插件即可\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
