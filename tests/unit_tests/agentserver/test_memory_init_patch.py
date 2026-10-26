# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""空记忆库拷模板、建库离开事件循环、第一句回复不等建库。"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from pathlib import Path
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
async def test_coding_before_invoke_still_builds_memory_db(memory_patch, monkeypatch):
    """代码模式不推迟建库，自动召回仍可用。"""
    called = {"n": 0}

    async def _fake_init(*_args, **_kwargs):
        called["n"] += 1
        return object()

    monkeypatch.setattr(
        "openjiuwen.harness.rails.memory.coding_memory_rail.init_memory_manager_async",
        _fake_init,
    )
    rail = CodingMemoryRail.__new__(CodingMemoryRail)
    rail._manager_initialized = False
    rail._manager = None
    rail._tool_ctx = None
    rail._recalled_content = None
    rail._prefetch_task = None
    rail._coding_memory_dir = ""
    rail.workspace = None
    rail._embedding_config = None
    rail.sys_operation = None

    class _Agent:
        card = type("Card", (), {"id": "agent-1"})()

        def _get_llm(self):
            return None

    class _Inputs:
        def is_cron(self):
            return False

        def is_heartbeat(self):
            return False

    ctx = type("Ctx", (), {"inputs": _Inputs(), "agent": _Agent()})()
    await rail.before_invoke(ctx)
    assert called["n"] == 1
    assert rail._manager_initialized is True
    assert rail._manager is not None


def test_coding_memory_rail_is_not_deferred(memory_patch):
    """补丁不得替换 CodingMemoryRail 的建库入口。"""
    assert (
        CodingMemoryRail._init_coding_memory_manager
        is not memory_init_patch._defer_memory_index
    )


def test_template_stable_path_is_process_private():
    path = memory_init_patch._template_stable_path()
    assert f"jws_empty_memory_{os.getpid()}.db" in path


def test_copy_sqlite_file_removes_temp_on_failure(tmp_path, monkeypatch):
    """拷贝失败时清掉 .copying，避免临时文件残留。"""
    src = tmp_path / "src.db"
    src.write_bytes(b"ok")
    dest = tmp_path / "dest.db"
    copying = Path(str(dest) + ".copying")

    def _boom(_s, _d):
        copying.write_bytes(b"partial")
        raise OSError("disk full")

    monkeypatch.setattr(memory_init_patch.shutil, "copy2", _boom)
    with pytest.raises(OSError, match="disk full"):
        memory_init_patch._copy_sqlite_file(str(src), str(dest))
    assert not copying.exists()
    assert not dest.exists()


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


def test_adapter_init_defers_memory_only_on_enterprise() -> None:
    """个人版不装推迟建库补丁，保持上游原路径。"""
    src = (
        Path(__file__).resolve().parents[3]
        / "jiuwenswarm"
        / "server"
        / "runtime"
        / "agent_adapter"
        / "interface_deep.py"
    ).read_text(encoding="utf-8")
    start = src.find("apply_deepagent_task_plan_binding_patch()")
    body = src[start : start + 1200]
    enterprise_at = body.find("enterprise = is_enterprise()")
    if_at = body.find("if enterprise:")
    mem_at = body.find("apply_memory_init_patch()")
    ctx_at = body.find("apply_context_read_patch()")
    assert enterprise_at != -1
    assert if_at != -1
    assert mem_at != -1
    assert ctx_at != -1
    assert enterprise_at < if_at < mem_at < ctx_at


def test_non_enterprise_adapter_skips_memory_patches(monkeypatch):
    """运行时：非企业版构造 adapter 不安装 memory / context 补丁。"""
    import jiuwenswarm.server.runtime.agent_adapter.interface_deep as deep_mod

    calls: list[str] = []
    monkeypatch.setattr(deep_mod, "is_enterprise", lambda: False)
    monkeypatch.setattr(
        deep_mod, "apply_mcp_call_timeout_patch", lambda: None
    )
    monkeypatch.setattr(
        deep_mod, "apply_deepagent_task_plan_binding_patch", lambda: None
    )
    monkeypatch.setattr(
        deep_mod,
        "apply_memory_init_patch",
        lambda: calls.append("memory"),
    )
    monkeypatch.setattr(
        deep_mod,
        "apply_context_read_patch",
        lambda: calls.append("context"),
    )
    monkeypatch.setattr(deep_mod, "get_agent_workspace_dir", lambda: "/tmp/ws")
    monkeypatch.setattr(
        deep_mod,
        "collapse_nested_agent_workspace_dir",
        lambda path: path,
    )

    adapter = deep_mod.JiuWenSwarmDeepAdapter.__new__(deep_mod.JiuWenSwarmDeepAdapter)
    deep_mod.JiuWenSwarmDeepAdapter.__init__(adapter)
    assert calls == []


def test_enterprise_adapter_applies_memory_patches(monkeypatch):
    """运行时：企业版构造 adapter 会安装 memory / context 补丁。"""
    import jiuwenswarm.server.runtime.agent_adapter.interface_deep as deep_mod

    calls: list[str] = []
    monkeypatch.setattr(deep_mod, "is_enterprise", lambda: True)
    monkeypatch.setattr(
        deep_mod, "apply_mcp_call_timeout_patch", lambda: None
    )
    monkeypatch.setattr(
        deep_mod, "apply_deepagent_task_plan_binding_patch", lambda: None
    )
    monkeypatch.setattr(
        deep_mod,
        "apply_memory_init_patch",
        lambda: calls.append("memory"),
    )
    monkeypatch.setattr(
        deep_mod,
        "apply_context_read_patch",
        lambda: calls.append("context"),
    )
    monkeypatch.setattr(deep_mod, "get_agent_workspace_dir", lambda: "/tmp/ws")
    monkeypatch.setattr(
        deep_mod,
        "collapse_nested_agent_workspace_dir",
        lambda path: path,
    )

    adapter = deep_mod.JiuWenSwarmDeepAdapter.__new__(deep_mod.JiuWenSwarmDeepAdapter)
    deep_mod.JiuWenSwarmDeepAdapter.__init__(adapter, workspace_dir="/tmp/ws")
    assert calls == ["memory", "context"]
