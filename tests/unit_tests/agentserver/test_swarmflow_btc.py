# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""BackgroundTaskController dual-API adapter (legacy no-arg vs run_id)."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.agents.harness.team.swarmflow_btc import (
    pause_background_tasks,
    resume_background_tasks,
    stop_background_tasks,
)


class _Handle:
    """Minimal SwarmflowRunHandle stand-in for legacy abort paths."""

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        self.abort_event = asyncio.Event()
        self.backend = None
        self.native = None
        self.relaunched = 0

    def relaunch(self) -> None:
        self.relaunched += 1


class _LegacyController:
    """e50a4369-shaped BTC: pause()/resume()/is_paused() only, no stop."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._active: dict[str, Any] = {}
        self._paused: dict[str, Any] = {}

    async def pause(self) -> bool:
        async with self._lock:
            if not self._active:
                return False
            for task_id, handle in list(self._active.items()):
                handle.abort_event.set()
                self._paused[task_id] = handle.relaunch
                self._active.pop(task_id, None)
            return True

    async def resume(self) -> bool:
        async with self._lock:
            if not self._paused:
                return False
            for task_id, relaunch in list(self._paused.items()):
                relaunch()
                self._paused.pop(task_id, None)
            return True

    def is_paused(self) -> bool:
        return bool(self._paused)


class _ModernController:
    """Parameterized BTC: pause/resume/stop accept optional run_id."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def pause(self, run_id: str | None = None) -> bool:
        self.calls.append(("pause", run_id))
        return True

    async def resume(self, run_id: str | None = None) -> bool:
        self.calls.append(("resume", run_id))
        return True

    async def stop(self, run_id: str | None = None) -> bool:
        self.calls.append(("stop", run_id))
        return True


@pytest.mark.asyncio
async def test_modern_pause_resume_stop_forward_run_id() -> None:
    ctl = _ModernController()
    assert await pause_background_tasks(ctl, "wf_1") is True
    assert await resume_background_tasks(ctl, "wf_1") is True
    assert await stop_background_tasks(ctl, "wf_1") is True
    assert ctl.calls == [
        ("pause", "wf_1"),
        ("resume", "wf_1"),
        ("stop", "wf_1"),
    ]


@pytest.mark.asyncio
async def test_modern_none_run_id_targets_all() -> None:
    ctl = _ModernController()
    assert await pause_background_tasks(ctl, None) is True
    assert await stop_background_tasks(ctl, None) is True
    assert ctl.calls == [("pause", None), ("stop", None)]


@pytest.mark.asyncio
async def test_legacy_pause_scoped_run_id_without_typeerror() -> None:
    """Web buttons pass run_id; locked pin has no-arg pause — must not TypeError."""
    ctl = _LegacyController()
    handle = _Handle("wf_1")
    ctl._active["wf_1"] = handle
    ctl._active["wf_2"] = _Handle("wf_2")

    assert await pause_background_tasks(ctl, "wf_1") is True
    assert handle.abort_event.is_set()
    assert "wf_1" not in ctl._active
    assert "wf_1" in ctl._paused
    assert "wf_2" in ctl._active  # sibling untouched


@pytest.mark.asyncio
async def test_legacy_pause_none_parks_all() -> None:
    ctl = _LegacyController()
    ctl._active["wf_1"] = _Handle("wf_1")
    ctl._active["wf_2"] = _Handle("wf_2")

    assert await pause_background_tasks(ctl, None) is True
    assert ctl._active == {}
    assert set(ctl._paused) == {"wf_1", "wf_2"}


@pytest.mark.asyncio
async def test_legacy_resume_scoped_and_all() -> None:
    ctl = _LegacyController()
    h1 = _Handle("wf_1")
    h2 = _Handle("wf_2")
    ctl._paused["wf_1"] = h1.relaunch
    ctl._paused["wf_2"] = h2.relaunch

    assert await resume_background_tasks(ctl, "wf_1") is True
    assert h1.relaunched == 1
    assert h2.relaunched == 0
    assert "wf_1" not in ctl._paused
    assert "wf_2" in ctl._paused

    assert await resume_background_tasks(ctl, None) is True
    assert h2.relaunched == 1
    assert ctl._paused == {}


@pytest.mark.asyncio
async def test_legacy_stop_aborts_without_relaunch_ticket() -> None:
    """Pins without stop(): abort active handle and drop relaunch (seal semantics)."""
    ctl = _LegacyController()
    handle = _Handle("wf_1")
    ctl._active["wf_1"] = handle

    assert await stop_background_tasks(ctl, "wf_1") is True
    assert handle.abort_event.is_set()
    assert "wf_1" not in ctl._active
    assert "wf_1" not in ctl._paused


@pytest.mark.asyncio
async def test_legacy_stop_discards_already_paused() -> None:
    ctl = _LegacyController()
    handle = _Handle("wf_1")
    ctl._paused["wf_1"] = handle.relaunch

    assert await stop_background_tasks(ctl, "wf_1") is True
    assert "wf_1" not in ctl._paused
    assert handle.relaunched == 0


@pytest.mark.asyncio
async def test_legacy_miss_returns_false() -> None:
    ctl = _LegacyController()
    assert await pause_background_tasks(ctl, "missing") is False
    assert await resume_background_tasks(ctl, "missing") is False
    assert await stop_background_tasks(ctl, "missing") is False


@pytest.mark.asyncio
async def test_native_stop_without_run_id_param() -> None:
    """Rare shape: stop() exists but is no-arg — call without run_id."""

    class _StopNoArg:
        def __init__(self) -> None:
            self.stopped = 0

        async def stop(self) -> bool:
            self.stopped += 1
            return True

    ctl = _StopNoArg()
    assert await stop_background_tasks(ctl, "wf_1") is True
    assert ctl.stopped == 1


@pytest.mark.asyncio
async def test_missing_methods_return_false() -> None:
    empty = object()
    assert await pause_background_tasks(empty, "wf_1") is False
    assert await resume_background_tasks(empty, "wf_1") is False
    # stop falls through to legacy registry which is empty → False
    assert await stop_background_tasks(empty, "wf_1") is False


@pytest.mark.asyncio
async def test_legacy_abort_sessions_best_effort() -> None:
    ctl = _LegacyController()
    handle = _Handle("wf_1")
    handle.backend = AsyncMock()
    handle.backend.abort_sessions = AsyncMock(side_effect=RuntimeError("boom"))
    ctl._active["wf_1"] = handle

    assert await pause_background_tasks(ctl, "wf_1") is True
    assert "wf_1" in ctl._paused
