from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from jiuwenclaw.extensions.types import ExtensionConfig, ExtensionMetadata

MANIFEST_FILENAME = "extension.yaml"


def _manifest_path(root: Path) -> Path | None:
    p = root / MANIFEST_FILENAME
    return p if p.exists() else None


class BaseExtension(ABC):
    _metadata_cache: Optional[ExtensionMetadata] = None
    _extension_dir: Optional[Path] = None
    _config_cache: Optional[dict] = None

    @abstractmethod
    async def initialize(self, config: ExtensionConfig) -> None:
        """扩展初始化

        Args:
            config: 扩展配置对象，包含全局配置和 logger
                   扩展可通过 self._load_config_from_yaml() 加载自己的 config.yaml
        """
        pass

    @abstractmethod
    async def shutdown(self) -> None:
        """扩展关闭

        用于释放扩展占用的资源
        """
        pass

    @property
    def metadata(self) -> ExtensionMetadata:
        """扩展元数据

        默认从扩展目录下的 extension.yaml 加载，如果文件不存在或解析失败，
        子类可以覆盖此属性提供自定义实现。

        Returns:
            包含扩展信息的 ExtensionMetadata 对象
        """
        if self._metadata_cache is not None:
            return self._metadata_cache

        self._metadata_cache = self._load_metadata_from_yaml()
        return self._metadata_cache

    def _load_metadata_from_yaml(self) -> ExtensionMetadata:
        """从扩展目录的清单 YAML 加载元数据"""
        import yaml

        root = self._get_extension_dir()
        if root is None:
            raise ValueError(
                "无法确定扩展目录，请在子类中设置目录或调用 set_extension_dir，或覆盖 metadata 属性"
            )

        yaml_path = _manifest_path(root)
        if yaml_path is None:
            raise FileNotFoundError(
                f"扩展元数据文件不存在（期望 {MANIFEST_FILENAME}）: {root}"
            )

        with open(yaml_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        return ExtensionMetadata(
            id=data.get("id", ""),
            name=data.get("name", ""),
            version=data.get("version", ""),
            description=data.get("description", ""),
            author=data.get("author", ""),
            min_jiuwenclaw_version=data.get("min_jiuwenclaw_version", ""),
            dependencies=data.get("dependencies", {}),
            config_schema=data.get("config_schema"),
        )

    def _get_extension_dir(self) -> Optional[Path]:
        """获取扩展包根目录路径"""
        if self._extension_dir is not None:
            return self._extension_dir

        import inspect

        cls = type(self)
        module = inspect.getmodule(cls)
        if module and hasattr(module, "__file__") and module.__file__:
            candidate = Path(module.__file__).parent
            if _manifest_path(candidate) is not None:
                return candidate

        return None

    def set_extension_dir(self, path: Path) -> None:
        """手动设置扩展根目录（含清单 YAML）"""
        self._extension_dir = path
        self._metadata_cache = None
        self._config_cache = None

    def _load_config_from_yaml(self) -> dict:
        """从扩展目录的 config.yaml 加载配置

        Returns:
            配置字典，如果文件不存在则返回空字典
        """
        if self._config_cache is not None:
            return self._config_cache

        import yaml

        root = self._get_extension_dir()
        if root is None:
            return {}

        config_path = root / "config.yaml"
        if not config_path.exists():
            return {}

        with open(config_path, "r", encoding="utf-8") as f:
            self._config_cache = yaml.safe_load(f) or {}

        return self._config_cache
