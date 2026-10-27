# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
"""配置域 Repository 与存储装配接线。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gateway.config.channel.access import (
    clear_channel_config_repository,
    get_channel_config_repository,
)
from gateway.config.enterprise.catalog import ENTERPRISE_RECORD_STORE_NAMES
from gateway.config.logging.access import (
    clear_logging_config_repository,
    get_logging_config_repository,
)
from gateway.config.permissions.access import (
    clear_permissions_config_repository,
    get_permissions_config_repository,
)
from gateway.routing.session_map_access import (
    clear_session_map_repository,
    get_session_map_repository,
)
from gateway.storage.access import clear_persistent_store, get_persistent_store
from gateway.storage.backends.memory_ephemeral import MemoryEphemeralBackend
from gateway.storage.context import StorageContext
from gateway.storage_assembly.setup import (
    create_channel_config_repository,
    create_enterprise_record_repositories,
    create_logging_config_repository,
    create_permissions_config_repository,
    is_storage_repositories_enabled,
    setup_gateway_storage_repositories,
    start_gateway_storage,
    teardown_gateway_storage_repositories,
)


class _DictPersistentStore:
    """测试用 dict PersistentStore（不作为正式 backend 导出）。"""

    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {}

    async def ensure_ready(self) -> None:
        return None

    async def close(self) -> None:
        return None

    def _rows(self, name: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(name, [])

    @staticmethod
    def _matches(row: dict[str, Any], key: dict[str, Any]) -> bool:
        return all(row.get(k) == v for k, v in key.items())

    async def get(self, name: str, key: dict[str, Any]) -> dict[str, Any] | None:
        for row in self._rows(name):
            if self._matches(row, key):
                return dict(row)
        return None

    async def create(self, name: str, record: dict[str, Any]) -> dict[str, Any]:
        row = dict(record)
        self._rows(name).append(row)
        return dict(row)

    async def update(
        self,
        name: str,
        key: dict[str, Any],
        updates: dict[str, Any],
    ) -> dict[str, Any] | None:
        for index, row in enumerate(self._rows(name)):
            if self._matches(row, key):
                merged = dict(row)
                merged.update(updates)
                self._rows(name)[index] = merged
                return dict(merged)
        return None

    async def delete(self, name: str, key: dict[str, Any]) -> bool:
        rows = self._rows(name)
        for index, row in enumerate(rows):
            if self._matches(row, key):
                rows.pop(index)
                return True
        if not key:
            if rows:
                rows.clear()
                return True
            return False
        return False

    async def list(
        self,
        name: str,
        *,
        filters: dict[str, Any] | None = None,
        order_by: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        _ = order_by
        rows = [dict(row) for row in self._rows(name)]
        if filters:
            rows = [row for row in rows if self._matches(row, filters)]
        if offset:
            rows = rows[offset:]
        if limit is not None:
            rows = rows[:limit]
        return rows


def _test_ctx() -> StorageContext:
    return StorageContext(
        _DictPersistentStore(),
        ephemeral_factory=lambda ns: MemoryEphemeralBackend(ns),
    )


@pytest.fixture(autouse=True)
def _clear_injections():
    yield
    clear_channel_config_repository()
    clear_permissions_config_repository()
    clear_logging_config_repository()
    clear_session_map_repository()
    clear_persistent_store()


@pytest.mark.asyncio
async def test_yaml_section_roundtrip() -> None:
    store = _DictPersistentStore()
    repo = create_logging_config_repository(store)
    await repo.replace({"level": "INFO", "gateway": "DEBUG"})
    body = await repo.get_body()
    assert body["level"] == "INFO"
    assert body["gateway"] == "DEBUG"


@pytest.mark.asyncio
async def test_create_star_uses_yaml_when_personal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)
    store = _DictPersistentStore()
    channel = create_channel_config_repository(store)
    perms = create_permissions_config_repository(store)
    logging_repo = create_logging_config_repository(store)
    assert channel is not None
    assert perms is not None
    assert logging_repo is not None
    await logging_repo.replace({"level": "INFO"})
    assert (await logging_repo.get_body())["level"] == "INFO"


def test_enterprise_record_repositories_cover_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    store = _DictPersistentStore()
    repos = create_enterprise_record_repositories(store)
    assert set(repos) == set(ENTERPRISE_RECORD_STORE_NAMES)


def test_storage_repositories_flag_default_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GATEWAY_STORAGE_REPOSITORIES", raising=False)
    assert is_storage_repositories_enabled({}) is True


@pytest.mark.asyncio
async def test_setup_wires_and_teardown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)
    monkeypatch.delenv("GATEWAY_STORAGE_REPOSITORIES", raising=False)
    monkeypatch.delenv("GATEWAY_SESSION_MAP_REPOSITORY", raising=False)
    ctx = _test_ctx()
    monkeypatch.setattr(
        "gateway.storage_assembly.setup.create_gateway_storage_context",
        lambda cfg=None: ctx,
    )
    wired = await setup_gateway_storage_repositories({})
    assert wired is ctx
    assert get_channel_config_repository() is not None
    assert get_permissions_config_repository() is not None
    assert get_logging_config_repository() is not None
    assert get_session_map_repository() is not None
    assert get_persistent_store() is not None

    await teardown_gateway_storage_repositories(ctx)
    assert get_channel_config_repository() is None
    assert get_session_map_repository() is None
    assert get_persistent_store() is None


@pytest.mark.asyncio
async def test_setup_skipped_when_flags_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_STORAGE_REPOSITORIES", "0")
    monkeypatch.setenv("GATEWAY_SESSION_MAP_REPOSITORY", "0")
    assert await setup_gateway_storage_repositories({}) is None
    assert get_channel_config_repository() is None


@pytest.mark.asyncio
async def test_start_gateway_storage_skips_personal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)
    called = {"n": 0}

    async def _setup(cfg=None):
        called["n"] += 1
        return _test_ctx()

    monkeypatch.setattr(
        "gateway.storage_assembly.setup.setup_gateway_storage_repositories",
        _setup,
    )
    assert await start_gateway_storage({}) is None
    assert called["n"] == 0
    assert get_channel_config_repository() is None
    assert get_persistent_store() is None


@pytest.mark.asyncio
async def test_start_gateway_storage_wires_enterprise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.delenv("GATEWAY_STORAGE_REPOSITORIES", raising=False)
    monkeypatch.delenv("GATEWAY_SESSION_MAP_REPOSITORY", raising=False)
    ctx = _test_ctx()
    monkeypatch.setattr(
        "gateway.storage_assembly.setup.create_gateway_storage_context",
        lambda cfg=None: ctx,
    )
    wired = await start_gateway_storage({})
    try:
        assert wired is ctx
        assert get_channel_config_repository() is not None
        assert get_permissions_config_repository() is not None
        assert get_logging_config_repository() is not None
        assert get_session_map_repository() is not None
        assert get_persistent_store() is not None
        from gateway.config.enterprise.access import (
            get_enterprise_record_repository,
        )
        from gateway.config.memory.access import (
            get_memory_config_repository,
        )

        assert get_memory_config_repository() is not None
        assert get_enterprise_record_repository("cron_job") is not None
    finally:
        if wired is not None:
            await teardown_gateway_storage_repositories(wired)


def test_enterprise_schema_catalogue_covers_audit_and_tenant_cron() -> None:
    from gateway.config.enterprise.tables.table_init import ALL_TABLE_DEFINITIONS

    definitions = {table.table_name: table for table in ALL_TABLE_DEFINITIONS}
    assert "audit_log_config" in definitions
    assert "audit_log_config" in ENTERPRISE_RECORD_STORE_NAMES
    cron_columns = {column.name for column in definitions["cron_job"].columns}
    assert {"job_id", "user_id", "group_id", "bot_id"}.issubset(cron_columns)
