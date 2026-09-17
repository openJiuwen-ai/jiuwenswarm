from pathlib import Path
from typing import Any

from jiuwenswarm.common.config import get_config
from jiuwenswarm.extensions.loader import ExtensionLoader
from jiuwenswarm.extensions.registry import ExtensionRegistry
from jiuwenswarm.common.utils import logger


_DEFAULT_PACKAGE_EXTENSION_DIR = ("jiuwenswarm", "extensions")
_DEFAULT_EXTENSION_DIR = "/".join(_DEFAULT_PACKAGE_EXTENSION_DIR)


def _is_default_package_extension_dir(path: Path) -> bool:
    parts = tuple(part.lower() for part in path.parts if part not in ("", "."))
    return parts == _DEFAULT_PACKAGE_EXTENSION_DIR


def _extension_search_path_candidates(path_value: str) -> list[Path]:
    path = Path(path_value)
    if path.is_absolute():
        return [path]

    candidates = [path.resolve()]
    if _is_default_package_extension_dir(path):
        candidates.append(Path(__file__).resolve().parent)
    return candidates


def _split_extension_dirs(value: str) -> list[str]:
    # 按需求使用 ';' 分割
    return [p.strip() for p in value.split(";") if p.strip()]


def _dedupe_extension_dirs(paths: list[str]) -> list[str]:
    seen: set[tuple[str, ...]] = set()
    result: list[str] = []
    for path in paths:
        key = tuple(part.lower() for part in Path(path).parts if part not in ("", "."))
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def _extension_dir_paths_from_config(cfg: dict) -> list[str]:
    """读取 ``extensions.extension_dirs``（扩展包搜索目录：仅支持字符串，用 ';' 分割）。"""
    ext = cfg.get("extensions")
    dirs = ext.get("extension_dirs") if isinstance(ext, dict) else None
    paths = _split_extension_dirs(dirs) if isinstance(dirs, str) else []
    paths.append(_DEFAULT_EXTENSION_DIR)
    return _dedupe_extension_dirs(paths)


class ExtensionManager:
    def __init__(
        self,
        registry: ExtensionRegistry,
    ):
        self.registry = registry
        self.loader = ExtensionLoader(registry)
        self._loaded_extensions: list[Any] = []
        self._setup_search_paths()

    def _setup_search_paths(self) -> None:
        seen: set[str] = set()
        extension_dirs = _extension_dir_paths_from_config(get_config())
        for path in extension_dirs:
            for p in _extension_search_path_candidates(path):
                if not p.exists():
                    continue
                key = str(p.resolve()).lower()
                if key in seen:
                    continue
                seen.add(key)
                self.loader.add_search_path(p)

    async def load_all_extensions(self) -> None:
        roots = self.loader.discover_extension_roots()
        logger.info("[ExtensionManager] 发现扩展路径: %s", roots)
        for path in roots:
            try:
                loaded = await self.loader.load_extension(path)
                if loaded:
                    logger.info("[ExtensionManager] 加载 %s", loaded)
                    if isinstance(loaded, list):
                        self._loaded_extensions.extend(loaded)
                    else:
                        self._loaded_extensions.append(loaded)
            except Exception as e:
                logger.error("[ExtensionManager] 加载扩展 %s 失败: %s", path, e)

    async def shutdown_all_extensions(self) -> None:
        for ext in self._loaded_extensions:
            try:
                if hasattr(ext, "shutdown"):
                    await ext.shutdown()
            except Exception as e:
                logger.warning("[ExtensionManager] 关闭扩展失败: %s, error=%s", ext, e)
        self._loaded_extensions.clear()

    def list_extensions(self) -> list[dict]:
        return [
            {"id": p.metadata.id, "name": p.metadata.name, "version": p.metadata.version}
            for p in self._loaded_extensions
            if hasattr(p, "metadata")
        ]
