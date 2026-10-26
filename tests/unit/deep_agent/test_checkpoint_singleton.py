# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Tests for per-agent checkpoint setup in interface_deep."""

from __future__ import annotations

import asyncio
import warnings
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

warnings.filterwarnings(
    "ignore",
    message=r"Protobuf gencode version.*",
)

from jiuwenswarm.server.runtime.agent_adapter import interface_deep as iface
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


@pytest.fixture(autouse=True)
def _reset_checkpoint_singleton():
    iface.reset_shared_checkpoint_for_tests()
    yield
    iface.reset_shared_checkpoint_for_tests()


def _make_adapter() -> JiuWenSwarmDeepAdapter:
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._checkpointer = None
    adapter._checkpoint_init_lock = asyncio.Lock()
    adapter._stream_event_rail = None
    adapter._env_service_id = "svc"
    adapter._env_agent_id = "agent"
    adapter._service_id = "svc"
    adapter._agent_id = "agent"
    return adapter


@pytest.mark.asyncio
async def test_set_checkpoint_initializes_once(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("CHECKPOINT_DB_TYPE", raising=False)
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)
    mock_checkpointer = MagicMock(name="checkpointer")
    adapter = _make_adapter()

    with patch.object(
        iface.CheckpointerFactory,
        "create",
        new=AsyncMock(return_value=mock_checkpointer),
    ) as create_mock, patch.object(
        iface.CheckpointerFactory,
        "set_default_checkpointer",
    ) as set_default_mock, patch.object(
        iface,
        "_build_mysql_async_engine",
        new=AsyncMock(),
    ) as mysql_mock, patch.object(
        iface,
        "get_checkpoint_dir",
        return_value=tmp_path,
    ):
        await adapter.set_checkpoint()
        await adapter.set_checkpoint()

    assert create_mock.await_count == 1
    assert adapter._checkpointer is mock_checkpointer
    # Per-agent checkpointers must not overwrite the process-wide default.
    set_default_mock.assert_not_called()
    mysql_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_checkpoint_reuses_mysql_engine(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.setenv("CHECKPOINT_DB_TYPE", "mysql")
    mock_engine = MagicMock(name="mysql_engine")
    mock_checkpointer = MagicMock(name="checkpointer")
    adapter = _make_adapter()

    with patch.object(
        iface,
        "_build_mysql_async_engine",
        new=AsyncMock(return_value=mock_engine),
    ) as mysql_mock, patch.object(
        iface.CheckpointerFactory,
        "create",
        new=AsyncMock(return_value=mock_checkpointer),
    ) as create_mock, patch.object(
        iface,
        "get_checkpoint_dir",
        return_value=tmp_path,
    ):
        await asyncio.gather(
            adapter.set_checkpoint(),
            adapter.set_checkpoint(),
            adapter.set_checkpoint(),
        )

    assert mysql_mock.await_count == 1
    assert create_mock.await_count == 1
    assert adapter._checkpointer is mock_checkpointer
    create_conf = create_mock.await_args_list[0].args[0].conf
    assert create_conf["db_client"] is mock_engine


def test_gateway_db_pool_kwargs_from_env(monkeypatch):
    monkeypatch.setenv("GATEWAY_DB_POOL_SIZE", "3")
    monkeypatch.setenv("GATEWAY_DB_MAX_OVERFLOW", "7")
    monkeypatch.setenv("GATEWAY_DB_POOL_TIMEOUT", "15")
    kwargs = iface._gateway_db_pool_kwargs()
    assert kwargs["pool_size"] == 3
    assert kwargs["max_overflow"] == 7
    assert kwargs["pool_timeout"] == 15
    assert kwargs["pool_pre_ping"] is True


def test_gateway_db_pool_kwargs_runtime_env_fallback(monkeypatch):
    monkeypatch.delenv("GATEWAY_DB_POOL_SIZE", raising=False)
    monkeypatch.delenv("GATEWAY_DB_MAX_OVERFLOW", raising=False)
    monkeypatch.delenv("GATEWAY_DB_POOL_TIMEOUT", raising=False)
    monkeypatch.setenv("RUNTIME_DB_POOL_SIZE", "4")
    monkeypatch.setenv("RUNTIME_DB_MAX_OVERFLOW", "8")
    monkeypatch.setenv("RUNTIME_DB_POOL_TIMEOUT", "12")
    kwargs = iface._gateway_db_pool_kwargs()
    assert kwargs["pool_size"] == 4
    assert kwargs["max_overflow"] == 8
    assert kwargs["pool_timeout"] == 12


@pytest.mark.asyncio
async def test_build_mysql_engine_reuses_process_singleton(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.setenv("GATEWAY_DB_HOST", "db.example")
    mock_engine = MagicMock(name="mysql_engine")
    iface._shared_mysql_checkpoint_engine = mock_engine

    with patch(
        "sqlalchemy.ext.asyncio.create_async_engine",
        new=MagicMock(),
    ) as create_engine:
        engine = await iface._build_mysql_async_engine()

    assert engine is mock_engine
    create_engine.assert_not_called()


@pytest.mark.asyncio
async def test_build_postgresql_engine_reuses_process_singleton(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.setenv("GATEWAY_DB_HOST", "db.example")
    mock_engine = MagicMock(name="pg_engine")
    iface._shared_postgresql_checkpoint_engine = mock_engine

    with patch(
        "sqlalchemy.ext.asyncio.create_async_engine",
        new=MagicMock(),
    ) as create_engine:
        engine = await iface._build_postgresql_async_engine()

    assert engine is mock_engine
    create_engine.assert_not_called()
