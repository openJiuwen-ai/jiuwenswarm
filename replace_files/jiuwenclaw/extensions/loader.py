from __future__ import annotations

import os
import importlib
import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

from jiuwenclaw.extensions.registry import ExtensionRegistry
from jiuwenclaw.utils import logger

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


class ExtensionLoader:
    def __init__(self, registry: ExtensionRegistry):
        self.registry = registry
        self._search_paths: list[Path] = []

    def add_search_path(self, path: Path) -> None:
        if path.exists():
            self._search_paths.append(path)

    def discover_extension_roots(self) -> list[Path]:
        """发现所有扩展根目录，按 priority 排序（数值越小越先加载）"""
        roots_with_priority: list[tuple[Path, int]] = []
        logger.info("[ExtensionLoader] 开始搜索扩展路径: %s", self._search_paths)
        for base_path in self._search_paths:
            if not base_path.exists():
                continue
            for subdir in base_path.iterdir():
                if not subdir.is_dir():
                    continue
                if _is_extension_root(subdir):
                    # 读取 priority 字段，默认为 10
                    manifest = _load_manifest_dict(subdir)
                    priority = manifest.get("priority", 10)
                    roots_with_priority.append((subdir, priority))

        # 按 priority 排序（数值越小越先加载）
        sorted_roots = sorted(roots_with_priority, key=lambda x: x[1])
        logger.info(
            "[ExtensionLoader] 扩展加载顺序: %s",
            [(r.name, p) for r, p in sorted_roots]
        )
        return [root for root, _ in sorted_roots]

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

        import shutil
        import subprocess
        import sys

        uv_path = shutil.which("uv")
        use_uv = uv_path is not None

        pip_extra_args = os.environ.get("PIP_EXTRA_ARGS", "").strip().strip('\'"')
        extra_args = pip_extra_args.split() if pip_extra_args else []

        for package, version_spec in dependencies.items():
            package_name = f"{package}{version_spec}" if version_spec else package
            try:
                importlib.metadata.version(package)
                logger.info(f"[ExtensionLoader] 扩展 {root.name} 依赖 {package} 已安装")
                continue
            except importlib.metadata.PackageNotFoundError:
                pass

            logger.info(f"[ExtensionLoader] 正在安装扩展 {root.name} 的依赖: {package_name}")
            try:
                if use_uv:
                    subprocess.check_call(
                        [uv_path, "pip", "install", package_name] + extra_args,
                        stdout=subprocess.DEVNULL,
                        stderr=None,
                        timeout=600,
                    )
                else:
                    subprocess.check_call(
                        [sys.executable, "-m", "pip", "install", package_name] + extra_args,
                        stdout=subprocess.DEVNULL,
                        stderr=None,
                        timeout=600,
                    )
                logger.info(f"[ExtensionLoader] 扩展 {root.name} 依赖 {package} 安装成功")
            except subprocess.TimeoutExpired:
                logger.error(f"[ExtensionLoader] 扩展 {root.name} 依赖 {package} 安装超时 (120秒)")
            except subprocess.CalledProcessError as e:
                logger.error(f"[ExtensionLoader] 扩展 {root.name} 依赖 {package} 安装失败: {e}")

    @staticmethod
    def _import_module(root: Path) -> Any:
        entry = _find_entry_script(root)
        if entry is None:
            raise FileNotFoundError(
                f"扩展入口脚本不存在（期望 {ENTRY_FILENAME}）: {root}"
            )

        module_name = root.name
        full_name = f"jiuwenclaw.loaded_extension.{module_name}"

        # 合成包名下的相对导入（如 extension.py 中 from .foo import ...）需要父包
        # jiuwenclaw.loaded_extension 存在于 sys.modules，且本模块需带 __path__ 才能解析同目录子模块。
        parent = "jiuwenclaw.loaded_extension"
        if parent not in sys.modules:
            pkg = types.ModuleType(parent)
            pkg.__path__ = []
            sys.modules[parent] = pkg

        spec = importlib.util.spec_from_file_location(full_name, entry)
        if spec is None or spec.loader is None:
            raise ImportError(f"无法加载扩展: {module_name}")

        module = importlib.util.module_from_spec(spec)
        root_str = str(root.resolve())
        module.__path__ = [root_str]
        module.__package__ = full_name
        sys.modules[full_name] = module
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
