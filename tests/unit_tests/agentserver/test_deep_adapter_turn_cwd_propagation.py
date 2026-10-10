# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Cwd changes must reach tasks started before the current turn."""

from __future__ import annotations

import asyncio
import contextvars

import pytest
from openjiuwen.core.sys_operation.cwd import get_cwd, get_project_root, get_workspace

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)


def _make_adapter(session_id: str, workspace_dir: str) -> JiuWenSwarmDeepAdapter:
    """Build a bare adapter holding only the attributes the cwd seed reads."""
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._parent_session_id = session_id
    adapter._project_dir = None
    adapter._workspace_dir = workspace_dir
    return adapter


@pytest.mark.asyncio
async def test_turn_cwd_reaches_a_task_created_before_it(tmp_path):
    """A scheduler task started at setup sees a later turn's cwd change."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    moved_dir = tmp_path / "moved"
    moved_dir.mkdir()
    adapter = _make_adapter("sess", str(session_dir))

    adapter._seed_runtime_cwd(str(session_dir), workspace=str(session_dir))

    reseeded = asyncio.Event()
    observed: dict[str, str | None] = {}

    async def scheduler() -> None:
        observed["at_start"] = get_cwd()
        await reseeded.wait()
        observed["after_reseed"] = get_cwd()
        observed["project_root"] = get_project_root()
        observed["workspace"] = get_workspace()

    scheduler_task = asyncio.create_task(scheduler())
    await asyncio.sleep(0)

    adapter._reseed_runtime_cwd(str(moved_dir), workspace=str(session_dir))
    reseeded.set()
    await scheduler_task

    assert observed["at_start"] == str(session_dir)
    assert observed["after_reseed"] == str(moved_dir), (
        "a turn's cwd must mutate the CwdState the scheduler task already holds"
    )
    # Moving cwd must not change the workspace or project root.
    assert observed["project_root"] == str(session_dir)
    assert observed["workspace"] == str(session_dir)


@pytest.mark.asyncio
async def test_reseed_falls_back_to_a_full_seed_without_inherited_state(tmp_path):
    """A task without CwdState gets a complete new binding."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    adapter = _make_adapter("sess", str(session_dir))

    async def turn() -> tuple[str, str | None]:
        adapter._reseed_runtime_cwd(str(session_dir), workspace=str(session_dir))
        return get_cwd(), get_workspace()

    # An empty Context avoids CwdState left by another test.
    cwd, workspace = await asyncio.create_task(turn(), context=contextvars.Context())

    assert cwd == str(session_dir)
    assert workspace == str(session_dir), "the fallback must bind the workspace layer too"


@pytest.mark.asyncio
async def test_subagent_startup_keeps_replace_semantics(tmp_path):
    """A child agent's new CwdState leaves its parent's cwd intact."""
    parent_dir = tmp_path / "parent"
    parent_dir.mkdir()
    child_dir = tmp_path / "child"
    child_dir.mkdir()
    parent = _make_adapter("sess", str(parent_dir))
    child = _make_adapter("sess_child", str(child_dir))

    parent._seed_runtime_cwd(str(parent_dir), workspace=str(parent_dir))

    async def child_agent() -> str:
        child._seed_runtime_cwd(str(child_dir), workspace=str(child_dir))
        return get_cwd()

    child_cwd = await asyncio.create_task(child_agent())

    assert child_cwd == str(child_dir)
    assert get_cwd() == str(parent_dir), "a child agent's seed must not move the parent"


@pytest.mark.asyncio
async def test_turn_in_new_context_updates_existing_scheduler(tmp_path):
    """A later request can arrive without the scheduler's CwdState binding."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    moved_dir = tmp_path / "moved"
    moved_dir.mkdir()
    adapter = _make_adapter("sess", str(session_dir))
    adapter._seed_runtime_cwd(str(session_dir), workspace=str(session_dir))

    resume = asyncio.Event()

    async def scheduler() -> str:
        await resume.wait()
        return get_cwd()

    scheduler_task = asyncio.create_task(scheduler())
    await asyncio.sleep(0)

    async def later_turn() -> None:
        adapter._reseed_runtime_cwd(str(moved_dir), workspace=str(session_dir))

    await asyncio.create_task(later_turn(), context=contextvars.Context())
    resume.set()

    assert await scheduler_task == str(moved_dir)


@pytest.mark.asyncio
async def test_workspace_root_change_reaches_existing_scheduler(tmp_path):
    """The first projectless turn can replace the startup workspace root."""
    startup_dir = tmp_path / "startup"
    startup_dir.mkdir()
    task_root = tmp_path / "task"
    task_root.mkdir()
    task_cwd = task_root / "work"
    task_cwd.mkdir()
    adapter = _make_adapter("sess", str(startup_dir))
    adapter._seed_runtime_cwd(str(startup_dir), workspace=str(startup_dir))

    resume = asyncio.Event()

    async def scheduler() -> tuple[str, str, str | None]:
        await resume.wait()
        return get_cwd(), get_project_root(), get_workspace()

    scheduler_task = asyncio.create_task(scheduler())
    await asyncio.sleep(0)

    async def first_turn() -> None:
        adapter._reseed_runtime_cwd(str(task_cwd), workspace=str(task_root))

    await asyncio.create_task(first_turn(), context=contextvars.Context())
    resume.set()

    assert await scheduler_task == (str(task_cwd), str(task_root), str(task_root))


def test_cwd_binding_does_not_retain_other_context_values(tmp_path):
    private = contextvars.ContextVar("private", default=None)
    token = private.set(object())
    try:
        adapter = _make_adapter("sess", str(tmp_path))
        adapter._seed_runtime_cwd(str(tmp_path), workspace=str(tmp_path))
        bindings = dict(adapter._runtime_cwd_context.items())
    finally:
        private.reset(token)

    assert private not in bindings
    assert len(bindings) == 1
