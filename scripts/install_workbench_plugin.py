# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Package and deploy the webui_workbench_v2 demo plugin for local trials.

The script mirrors what the `/extension install` web command does, using the
same install-directory resolution as the running server:

1. Build ``webui_workbench_v2.zip`` at the repository root (same layout as the
   packaging snippet in the plugin README).
2. Copy the package source into ``<install_dir>/<package_id>/<version>/``
   via a staging directory, so the next service start activates it through
   ``ExtensionManager.load_installed``.

Usage (repository root):

    .venv/Scripts/python.exe scripts/install_workbench_plugin.py            # package + deploy
    .venv/Scripts/python.exe scripts/install_workbench_plugin.py --force    # replace existing copy
    .venv/Scripts/python.exe scripts/install_workbench_plugin.py --zip-only # only build the ZIP

After deployment either restart the debug service (``make start-debug``) or
run the web command ``/extension install <repo>/webui_workbench_v2.zip`` in
the single-agent input for a hot install without restart.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE = REPO_ROOT / "jiuwenswarm/extensions_v2/examples/webui_workbench_v2"
ZIP_PATH = REPO_ROOT / "webui_workbench_v2.zip"


def _resolve_install_dir() -> Path:
    """Reuse the server-side resolution so the copy lands where the service looks."""

    sys.path.insert(0, str(REPO_ROOT))
    from jiuwenswarm.extensions_v2.config import resolve_install_dir

    return resolve_install_dir()


def _load_manifest(root: Path):
    from openjiuwen.core.extension.manifest import load_manifest

    return load_manifest(root)


def build_zip() -> Path:
    """Package the plugin source into a ZIP with manifest.yaml at the root."""

    from zipfile import ZIP_DEFLATED, ZipFile

    with ZipFile(ZIP_PATH, "w", ZIP_DEFLATED) as output:
        for path in sorted(SOURCE.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                output.write(path, path.relative_to(SOURCE))
    return ZIP_PATH


def deploy(install_dir: Path, package_id: str, version: str, *, force: bool) -> Path:
    """Copy the package into ``<install_dir>/<package_id>/<version>`` atomically."""

    destination = install_dir / package_id / version
    if destination.exists():
        if not force:
            print(f"[skip] 已存在安装副本（用 --force 覆盖）: {destination}")
            return destination
        print(f"[force] 移除旧副本: {destination}")
        shutil.rmtree(destination)

    staging = destination.with_name(f"{destination.name}.staging-{id(destination)}")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        shutil.copytree(SOURCE, staging, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        staging.rename(destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description="Package and deploy webui_workbench_v2")
    parser.add_argument("--force", action="store_true", help="replace an existing installed copy")
    parser.add_argument("--zip-only", action="store_true", help="only build the ZIP, skip deployment")
    args = parser.parse_args()

    if not (SOURCE / "manifest.yaml").is_file():
        print(f"[error] 未找到插件源码: {SOURCE}")
        return 1

    manifest = _load_manifest(SOURCE)
    zip_path = build_zip()
    print(f"[zip] {zip_path}")

    if args.zip_only:
        return 0

    install_dir = _resolve_install_dir()
    destination = deploy(install_dir, manifest.package_id, manifest.version, force=args.force)
    print(f"[deploy] {destination}")

    print(
        "\n下一步（二选一）:\n"
        "  1. 重启调试服务后自动加载: make stop && make start-debug\n"
        f"  2. 不重启热安装，在 Web 单 Agent 输入框执行:\n"
        f"     /extension install {zip_path}\n"
        "安装后进入真实会话并刷新页面触发 ui.ready。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
