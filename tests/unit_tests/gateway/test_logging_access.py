# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import logging

import pytest

from jiuwenswarm.common.utils import reload_logging_levels
from jiuwenswarm.gateway.config.logging import LoggingConfigRepository, YamlSectionCodec
from jiuwenswarm.gateway.config.logging.access import (
    clear_logging_config_repository,
    get_logging_config_section_sync,
    set_logging_config_repository,
)
from jiuwenswarm.gateway.storage.backends.memory_persistent import InMemoryPersistentBackend


@pytest.mark.asyncio
async def test_get_logging_config_section_sync_uses_repository() -> None:
    store = InMemoryPersistentBackend()
    repo = LoggingConfigRepository(store, YamlSectionCodec())
    set_logging_config_repository(repo)
    try:
        await repo.replace(
            {"level": "INFO", "gateway": "DEBUG", "preview_user_content": False}
        )
        section = get_logging_config_section_sync()
        assert section["gateway"] == "DEBUG"
        assert section["preview_user_content"] is False
    finally:
        clear_logging_config_repository()


@pytest.mark.asyncio
async def test_reload_logging_levels_prefers_repository() -> None:
    store = InMemoryPersistentBackend()
    repo = LoggingConfigRepository(store, YamlSectionCodec())
    set_logging_config_repository(repo)
    try:
        await repo.merge_levels({"gateway": "ERROR", "level": "WARNING"})
        await reload_logging_levels()
        root = logging.getLogger("jiuwenswarm")
        assert root.level == logging.WARNING
    finally:
        clear_logging_config_repository()
        await reload_logging_levels()
