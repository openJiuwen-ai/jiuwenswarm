from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path
from typing import Any

from jiuwenswarm.extensions.registry import ExtensionRegistry
from jiuwenswarm.common.utils import logger

MANIFEST_FILENAME = "extension.yaml"
ENTRY_FILENAME = "extension.py"


def _find_manifest(root: Path) -> Path | None:
    p = root / MANIFEST_FILENAME
    return p if p.exists() else None


def _find_entry_script(root: Path) -> Path | None:
    p = root / ENTRY_FILENAME
    return p if p.exists() else None


def _is_extension_root(path: Path) -> bool:
    return _find_manifest(path) is not None or _find_entry_script(path) is not None


def _extension_display_name(manifest: dict, root: Path) -> str:
    name = str(manifest.get("name", "")).strip()
    return name or root.name


class ExtensionLoader:
    def __init__(self, registry: ExtensionRegistry):
        self.registry = registry
        self._search_paths: list[Path] = []

    def add_search_path(self, path: Path) -> None:
        if path.exists():
            self._search_paths.append(path)

    def discover_extension_roots(self) -> list[Path]:
        roots: list[Path] = []
        logger.info("[ExtensionLoader] 开始搜索扩展路径: %s", self._search_paths)
        for base_path in self._search_paths:
            if not base_path.exists():
                continue
            for subdir in base_path.iterdir():
                if not subdir.is_dir():
                    continue
                if _is_extension_root(subdir):
                    roots.append(subdir)
        return roots

    async def load_extension(self, root: Path) -> Any:
        manifest = _load_manifest_dict(root)

        await self._install_dependencies(manifest, root)

        module = self._import_module(root)

        if hasattr(module, "register_extensions"):
            registered = await module.register_extensions(self.registry)
        else:
            registered = None

        if registered:
            items = registered if isinstance(registered, list) else [registered]
            for ext in items:
                if hasattr(ext, "set_extension_dir"):
                    ext.set_extension_dir(root)
            return registered

        return None

    async def _install_dependencies(self, manifest: dict, root: Path) -> None:
        """安装扩展声明的依赖"""
        dependencies = manifest.get("dependencies", {})
        if not dependencies:
            return
        extension_name = _extension_display_name(manifest, root)

        import shutil
        import subprocess
        import sys

        uv_path = shutil.which("uv")
        use_uv = uv_path is not None

        for package, version_spec in dependencies.items():
            package_name = f"{package}{version_spec}" if version_spec else package
            try:
                importlib.metadata.version(package)
                logger.info(f"[ExtensionLoader] 扩展 {extension_name} 依赖 {package} 已安装")
                continue
            except importlib.metadata.PackageNotFoundError:
                pass

            logger.info(f"[ExtensionLoader] 正在安装扩展 {extension_name} 的依赖: {package_name}")
            try:
                if use_uv:
                    subprocess.check_call(
                        [uv_path, "pip", "install", package_name],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                        timeout=120,
                    )
                else:
                    subprocess.check_call(
                        [sys.executable, "-m", "pip", "install", package_name],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                        timeout=120,
                    )
                logger.info(f"[ExtensionLoader] 扩展 {extension_name} 依赖 {package} 安装成功")
            except subprocess.TimeoutExpired:
                logger.error(f"[ExtensionLoader] 扩展 {extension_name} 依赖 {package} 安装超时 (120秒)")
            except subprocess.CalledProcessError as e:
                logger.error(f"[ExtensionLoader] 扩展 {extension_name} 依赖 {package} 安装失败: {e}")

    @staticmethod
    def _import_module(root: Path) -> Any:
        entry = _find_entry_script(root)
        if entry is None:
            raise FileNotFoundError(
                f"扩展入口脚本不存在（期望 {ENTRY_FILENAME}）: {root}"
            )

        module_name = root.name
        spec = importlib.util.spec_from_file_location(
            f"jiuwenswarm.loaded_extension.{module_name}",
            entry,
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"无法加载扩展: {module_name}")

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


def _load_manifest_dict(root: Path) -> dict:
    manifest_path = _find_manifest(root)
    if manifest_path is None:
        return {}
    try:
        import yaml

        return yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except ImportError:
        return {}
