# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""空记忆库拷模板、建库离开事件循环、第一句回复不等建库。"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from typing import Optional

import pytest

from openjiuwen.core.memory.lite.config import MemorySettings
from openjiuwen.core.memory.lite.manager import (
    MemoryIndexManager,
    clear_memory_manager_cache,
)
from openjiuwen.harness.rails.memory.coding_memory_rail import CodingMemoryRail
from openjiuwen.harness.rails.memory.memory_rail import MemoryRail

from jiuwenswarm.server.runtime import memory_init_patch
from jiuwenswarm.server.runtime.memory_init_patch import (
    apply_memory_init_patch,
    remove_memory_init_patch,
)


class _DirWorkspace:
    """单测用的最小工作区，只提供记忆目录。"""

    def __init__(self, memory_dir: str) -> None:
        self.root_path = os.path.dirname(memory_dir)
        self.memory_dir = memory_dir
        self.daily_rel: Optional[str] = None

    def get_node_path(self, node_name: str) -> Optional[str]:
        if node_name == "memory":
            return self.memory_dir
        return None

    def get_directory(self, name: str) -> Optional[str]:
        if name == "daily_memory":
            return self.daily_rel
        return None


@pytest.fixture
def memory_patch():
    apply_memory_init_patch()
    yield
    remove_memory_init_patch()
    clear_memory_manager_cache()


@pytest.mark.asyncio
async def test_before_invoke_does_not_build_memory_db(memory_patch, monkeypatch):
    """第一句回复前不调用建库。"""
    called = {"n": 0}

    async def _boom(*_args, **_kwargs):
        called["n"] += 1
        raise AssertionError("should not init memory before the first reply")

    monkeypatch.setattr(
        "openjiuwen.harness.rails.memory.memory_rail.init_memory_manager_async",
        _boom,
    )
    rail = MemoryRail.__new__(MemoryRail)
    rail._initialized = False
    await rail.before_invoke(type("Ctx", (), {"inputs": object()})())
    assert called["n"] == 0
    assert rail._initialized is True


def test_seed_closes_db_when_schema_fails(memory_patch, tmp_path, monkeypatch):
    """建表失败时关掉已经打开的连接，避免泄漏。"""
    template = tmp_path / "template.db"
    template.write_bytes(b"")
    dest = tmp_path / "memory" / "memory.db"
    closed = {"n": 0}

    class _Conn:
        def close(self) -> None:
            closed["n"] += 1

    monkeypatch.setattr(memory_init_patch, "_ensure_template_db", lambda: str(template))
    monkeypatch.setattr(memory_init_patch, "_original_open_database", lambda _path: _Conn())

    def _boom(_manager):
        raise RuntimeError("schema failed")

    monkeypatch.setattr(memory_init_patch, "_original_ensure_schema", _boom)
    manager = MemoryIndexManager.__new__(MemoryIndexManager)
    manager.db = None
    manager.db_path = None
    with pytest.raises(RuntimeError, match="schema failed"):
        memory_init_patch._seed_empty_db(manager, str(dest))
    assert closed["n"] == 1
    assert manager.db is None


@pytest.mark.asyncio
async def test_coding_before_invoke_does_not_build_memory_db(memory_patch, monkeypatch):
    """代码模式记忆在第一句回复前也不调用建库。"""
    called = {"n": 0}

    async def _boom(*_args, **_kwargs):
        called["n"] += 1
        raise AssertionError("should not init coding memory before the first reply")

    monkeypatch.setattr(
        "openjiuwen.harness.rails.memory.coding_memory_rail.init_memory_manager_async",
        _boom,
    )
    rail = CodingMemoryRail.__new__(CodingMemoryRail)
    rail._manager_initialized = False
    rail._manager = None
    rail._recalled_content = None
    rail._prefetch_task = None
    await rail.before_invoke(type("Ctx", (), {"inputs": object()})())
    assert called["n"] == 0
    assert rail._manager_initialized is True


@pytest.mark.asyncio
async def test_missing_db_is_copied_off_the_event_loop(memory_patch, tmp_path, monkeypatch):
    """缺库时在线程里拷模板，而不是在事件循环上现场建。"""
    thread_names: list[str] = []
    real_to_thread = asyncio.to_thread

    async def _spy(func, *args, **kwargs):
        thread_names.append(getattr(func, "__name__", ""))
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(memory_init_patch.asyncio, "to_thread", _spy)
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    manager = MemoryIndexManager(
        "agent-new",
        _DirWorkspace(str(memory_dir)),
        MemorySettings(),
        "memory",
    )
    await manager.initialize()
    try:
        db_path = memory_dir / "memory.db"
        assert db_path.is_file()
        assert db_path.stat().st_size > 0
        assert "_seed_empty_db" in thread_names
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = ?",
                ("memory_index_meta_v1",),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert "none:no-embedding" in row[0]
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_existing_db_is_not_replaced(memory_patch, tmp_path, monkeypatch):
    """已经有库时不覆盖。"""
    thread_names: list[str] = []
    real_to_thread = asyncio.to_thread

    async def _spy(func, *args, **kwargs):
        thread_names.append(getattr(func, "__name__", ""))
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(memory_init_patch.asyncio, "to_thread", _spy)
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    db_path = memory_dir / "memory.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE marker (id INTEGER)")
    conn.execute("INSERT INTO marker VALUES (7)")
    conn.commit()
    conn.close()

    manager = MemoryIndexManager(
        "agent-old",
        _DirWorkspace(str(memory_dir)),
        MemorySettings(),
        "memory",
    )
    await manager.initialize()
    try:
        assert "_seed_empty_db" not in thread_names
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute("SELECT id FROM marker").fetchone()
        finally:
            conn.close()
        assert row == (7,)
    finally:
        await manager.close()
