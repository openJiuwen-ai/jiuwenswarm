# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from pathlib import Path
from typing import get_type_hints

import pytest

import jiuwenswarm.extensions as extensions
import jiuwenswarm.extensions.sdk as extension_sdk
from jiuwenswarm.extensions.manager import (
    ExtensionManager,
    _extension_dir_paths_from_config,
)
from jiuwenswarm.extensions.registry import ExtensionRegistry


def test_extension_dirs_default_to_builtin_when_empty_string() -> None:
    paths = _extension_dir_paths_from_config(
        {"extensions": {"extension_dirs": ""}}
    )

    assert paths == ["jiuwenswarm/extensions"]


def test_extension_dirs_default_to_builtin_when_missing() -> None:
    assert _extension_dir_paths_from_config({}) == ["jiuwenswarm/extensions"]
    assert _extension_dir_paths_from_config({"extensions": {}}) == [
        "jiuwenswarm/extensions"
    ]


def test_extension_dirs_append_builtin_after_custom_paths() -> None:
    paths = _extension_dir_paths_from_config(
        {"extensions": {"extension_dirs": "custom/a; custom/b "}}
    )

    assert paths == ["custom/a", "custom/b", "jiuwenswarm/extensions"]


def test_extension_dirs_do_not_duplicate_builtin_path() -> None:
    paths = _extension_dir_paths_from_config(
        {
            "extensions": {
                "extension_dirs": "custom/a;jiuwenswarm/extensions;custom/a"
            }
        }
    )

    assert paths == ["custom/a", "jiuwenswarm/extensions"]


def test_lazy_extension_exports_are_discoverable() -> None:
    assert set(extensions.__all__) <= set(dir(extensions))
    assert set(extension_sdk.__all__) <= set(dir(extension_sdk))


def test_registry_type_hints_resolve_without_transport_imports() -> None:
    methods = (
        ExtensionRegistry.register_agent_server_client,
        ExtensionRegistry.get_agent_server_client,
        ExtensionRegistry.register_third_agent,
        ExtensionRegistry.get_third_agent,
    )

    assert all(get_type_hints(method) for method in methods)


@pytest.mark.asyncio
async def test_transport_manifest_flag_requires_a_real_boolean() -> None:
    manifests = {
        Path("string-false"): {"requires_transport": "false"},
        Path("boolean-true"): {"requires_transport": True},
        Path("missing"): {},
    }

    class FakeLoader:
        @staticmethod
        def discover_extension_roots() -> list[Path]:
            return list(manifests)

        @staticmethod
        def load_manifest(path: Path) -> dict:
            return manifests[path]

        @staticmethod
        async def load_extension(path: Path, *, manifest: dict):
            return path

    manager = object.__new__(ExtensionManager)
    manager.loader = FakeLoader()
    manager._loaded_extensions = []

    await manager.load_all_extensions(include_transport_extensions=False)

    assert manager._loaded_extensions == [Path("string-false"), Path("missing")]

    remote_manager = object.__new__(ExtensionManager)
    remote_manager.loader = FakeLoader()
    remote_manager._loaded_extensions = []

    await remote_manager.load_all_extensions(include_transport_extensions=True)

    assert remote_manager._loaded_extensions == list(manifests)
