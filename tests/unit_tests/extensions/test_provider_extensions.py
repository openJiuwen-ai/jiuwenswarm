from pathlib import Path

import pytest

from jiuwenswarm.common import config_provider, path_provider
from jiuwenswarm.extensions.loader import ExtensionLoader
from jiuwenswarm.extensions.registry import ExtensionRegistry
from jiuwenswarm.extensions.sdk.config_provider import ConfigProviderExtension
from jiuwenswarm.extensions.sdk.path_provider import PathProviderExtension
from jiuwenswarm.extensions.types import ExtensionConfig


@pytest.fixture(autouse=True)
def reset_providers():
    path_provider.reset_path_provider()
    config_provider.reset_config_provider()
    yield
    path_provider.reset_path_provider()
    config_provider.reset_config_provider()


@pytest.mark.asyncio
async def test_sdk_extensions_forward_providers_to_common_registries(monkeypatch):
    class Paths(path_provider.PathProvider):
        name = "paths"

        def resolve_path(self, category, ctx, **kwargs):
            return (
                Path("custom") if category is path_provider.PathCategory.LOGS else None
            )

    class Config(config_provider.ConfigProvider):
        name = "config"

        def get_process_config(self):
            return {"custom": True}

    paths = Paths()
    config = Config()

    class PathExtension(PathProviderExtension):
        def get_path_provider(self):
            return paths

    class ConfigExtension(ConfigProviderExtension):
        def get_config_provider(self):
            return config

    registry = ExtensionRegistry.__new__(ExtensionRegistry)
    monkeypatch.setattr(
        ExtensionRegistry,
        "get_instance",
        classmethod(lambda cls: registry),
    )
    extension_config = ExtensionConfig(config={}, logger=None)

    await PathExtension().initialize(extension_config)
    await ConfigExtension().initialize(extension_config)

    assert path_provider.get_path_provider() is paths
    assert config_provider.get_config_provider() is config


@pytest.mark.asyncio
async def test_extension_yaml_registration_reaches_provider_registries(tmp_path):
    extension_root = tmp_path / "provider_extension"
    extension_root.mkdir()
    (extension_root / "extension.yaml").write_text(
        "id: provider-extension\nname: Provider Extension\nversion: 1.0.0\n",
        encoding="utf-8",
    )
    (extension_root / "extension.py").write_text(
        """
from pathlib import Path

from jiuwenswarm.common.config_provider import ConfigProvider
from jiuwenswarm.common.path_provider import PathCategory, PathProvider


class Paths(PathProvider):
    name = "manifest-paths"

    def resolve_path(self, category, ctx, **kwargs):
        return Path("manifest-logs") if category is PathCategory.LOGS else None


class Config(ConfigProvider):
    name = "manifest-config"

    def get_process_config(self):
        return {"manifest": True}


class PathExtension:
    def get_path_provider(self):
        return Paths()


class ConfigExtension:
    def get_config_provider(self):
        return Config()


async def register_extensions(registry):
    path_extension = PathExtension()
    config_extension = ConfigExtension()
    registry.register_path_provider(path_extension)
    registry.register_config_provider(config_extension)
    return [path_extension, config_extension]
""".lstrip(),
        encoding="utf-8",
    )
    registry = ExtensionRegistry.__new__(ExtensionRegistry)

    loaded = await ExtensionLoader(registry).load_extension(extension_root)

    assert len(loaded) == 2
    assert path_provider.get_path_provider().name == "manifest-paths"
    assert config_provider.get_config_provider().name == "manifest-config"
