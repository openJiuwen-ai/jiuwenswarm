# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
    _session_adapter_idle_ttl_default,
)


def _make_adapter(**state: object) -> JiuWenSwarmDeepAdapter:
    """Create a bare adapter with internal state set via setattr."""
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    state.setdefault("_sessions_awaiting_tool_permission", set())
    # Skill allow-list stamp bookkeeping (mirrors __init__; __new__ skips it).
    state.setdefault("_skill_enabled_version", 0)
    state.setdefault("_session_skill_versions", {})
    state.setdefault("_session_skill_refresh_failures", {})
    for name, value in state.items():
        setattr(adapter, name, value)
    return adapter


class _IdleChildAdapter:
    def __init__(self) -> None:
        self.cleaned = False
        self.released: list[tuple[str, str]] = []
        self._instance = None

    @staticmethod
    def is_session_active(_session_id: str) -> bool:
        return False

    @staticmethod
    def is_deep_agent_executing_for_session(_session_id: str) -> bool:
        return False

    async def stop_interaction(self) -> None:
        return None

    async def release_subagent_runtime_for_session(
        self,
        session_id: str,
        *,
        reason: str = "",
    ) -> None:
        self.released.append((session_id, reason))

    async def cleanup(self) -> None:
        self.cleaned = True


class _BlockingCleanupChildAdapter(_IdleChildAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.cleanup_started = asyncio.Event()
        self.cleanup_can_finish = asyncio.Event()

    async def cleanup(self) -> None:
        self.cleanup_started.set()
        await self.cleanup_can_finish.wait()
        await super().cleanup()


def test_idle_ttl_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIUWENSWARM_SESSION_ADAPTER_IDLE_TTL_SEC", "60")
    assert _session_adapter_idle_ttl_default() == 60


def test_idle_ttl_env_invalid_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    for bad in ("abc", "-1", "0", "   "):
        monkeypatch.setenv("JIUWENSWARM_SESSION_ADAPTER_IDLE_TTL_SEC", bad)
        assert _session_adapter_idle_ttl_default() == 2 * 60 * 60


def test_idle_ttl_env_absent_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JIUWENSWARM_SESSION_ADAPTER_IDLE_TTL_SEC", raising=False)
    assert _session_adapter_idle_ttl_default() == 2 * 60 * 60


def test_other_active_sessions_treats_subagent_as_related() -> None:
    adapter = _make_adapter(
        _active_session_ids={
            "tui_main": 1,
            "tui_main_sub_explore": 1,
        },
    )

    assert getattr(adapter, "_other_active_sessions")("tui_main") == 0
    assert getattr(adapter, "_other_active_sessions")("tui_main_sub_explore") == 0


def test_other_active_sessions_counts_unrelated_sessions() -> None:
    adapter = _make_adapter(
        _active_session_ids={
            "tui_a": 1,
            "tui_b": 1,
        },
    )

    assert getattr(adapter, "_other_active_sessions")("tui_a") == 1


@pytest.mark.asyncio
async def test_cancel_session_agent_tasks_cancels_registered_task() -> None:
    adapter = _make_adapter(_session_agent_tasks={})
    cancelled = asyncio.Event()

    async def worker() -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    task = asyncio.create_task(worker())
    getattr(adapter, "_session_agent_tasks")["sess_x"] = {task}
    await asyncio.sleep(0)

    cancelled_count = await getattr(adapter, "_cancel_session_agent_tasks")("sess_x")
    assert cancelled_count == 1
    await asyncio.wait_for(cancelled.wait(), timeout=2)


def test_is_session_live_when_deep_agent_stream_task_running() -> None:
    from unittest.mock import MagicMock

    instance = MagicMock()
    setattr(instance, "_invoke_active", True)
    stream_task = MagicMock()
    stream_task.done.return_value = False
    setattr(instance, "_stream_process_task", stream_task)
    loop_session = MagicMock()
    loop_session.get_session_id.return_value = "tui_main"
    setattr(instance, "_loop_session", loop_session)
    adapter = _make_adapter(
        _active_session_ids={},
        _session_agent_tasks={},
        _instance=instance,
    )

    assert getattr(adapter, "_is_session_live")("tui_main") is True
    assert getattr(adapter, "_other_active_sessions")("tui_other") == 1


@pytest.mark.asyncio
async def test_cleanup_session_adapter_removes_idle_child_adapter() -> None:
    child = _IdleChildAdapter()
    parent = _make_adapter(
        _is_session_scoped_adapter=False,
        _session_adapters={"sess_exit": child},
        _session_adapter_locks={"sess_exit": asyncio.Lock()},
        _session_adapter_last_used={"sess_exit": 1.0},
        _session_adapter_versions={"sess_exit": 1},
        _session_adapter_reload_failures={"sess_exit": (1, 1.0)},
        _session_skill_versions={"sess_exit": 1},
        _session_skill_refresh_failures={"sess_exit": (1, 1.0)},
    )

    removed = await getattr(parent, "cleanup_session_adapter")("sess_exit")

    assert removed is True
    assert child.cleaned is True
    assert getattr(parent, "_session_adapters") == {}
    assert getattr(parent, "_session_adapter_locks") == {}
    assert getattr(parent, "_session_adapter_last_used") == {}
    assert getattr(parent, "_session_adapter_versions") == {}
    assert getattr(parent, "_session_adapter_reload_failures") == {}
    assert getattr(parent, "_session_skill_versions") == {}
    assert getattr(parent, "_session_skill_refresh_failures") == {}


@pytest.mark.asyncio
async def test_cleanup_session_adapter_without_child_keeps_runtime_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    runtime_state_path = tmp_path / "sess_missing.yaml"
    runtime_state_path.write_text("mode: agent.plan\n", encoding="utf-8")
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep.get_runtime_state_path",
        lambda _session_id: runtime_state_path,
    )
    parent = _make_adapter(
        _is_session_scoped_adapter=False,
        _session_adapters={},
        _session_adapter_locks={"sess_missing": asyncio.Lock()},
        _session_adapter_last_used={"sess_missing": 1.0},
        _session_adapter_versions={"sess_missing": 1},
        _session_adapter_reload_failures={"sess_missing": (1, 1.0)},
    )

    removed = await getattr(parent, "cleanup_session_adapter")("sess_missing")

    assert removed is False
    assert runtime_state_path.exists()
    assert getattr(parent, "_session_adapter_locks") == {}


@pytest.mark.asyncio
async def test_cleanup_session_adapter_defers_inflight_child_creation() -> None:
    lock = asyncio.Lock()
    await lock.acquire()
    parent = _make_adapter(
        _is_session_scoped_adapter=False,
        _session_adapters={},
        _session_adapter_locks={"sess_race": lock},
        _session_adapter_last_used={"sess_race": 1.0},
        _session_adapter_versions={"sess_race": 1},
        _session_adapter_reload_failures={},
    )

    cleanup_task = asyncio.create_task(
        getattr(parent, "cleanup_session_adapter")("sess_race")
    )
    await asyncio.sleep(0)
    assert cleanup_task.done() is False

    child = _IdleChildAdapter()
    getattr(parent, "_session_adapters")["sess_race"] = child
    lock.release()

    removed = await asyncio.wait_for(cleanup_task, timeout=2)

    assert removed is False
    assert child.cleaned is False
    assert getattr(parent, "_session_adapters") == {"sess_race": child}


@pytest.mark.asyncio
async def test_cleanup_session_adapter_defers_locked_cached_child_adapter() -> None:
    lock = asyncio.Lock()
    await lock.acquire()
    child = _IdleChildAdapter()
    parent = _make_adapter(
        _is_session_scoped_adapter=False,
        _session_adapters={"sess_locked": child},
        _session_adapter_locks={"sess_locked": lock},
        _session_adapter_last_used={"sess_locked": 1.0},
        _session_adapter_versions={"sess_locked": 1},
        _session_adapter_reload_failures={},
    )

    cleanup_task = asyncio.create_task(
        getattr(parent, "cleanup_session_adapter")("sess_locked")
    )
    await asyncio.sleep(0)

    assert cleanup_task.done() is False
    assert child.cleaned is False

    lock.release()
    removed = await asyncio.wait_for(cleanup_task, timeout=2)

    assert removed is False
    assert child.cleaned is False
    assert getattr(parent, "_session_adapters") == {"sess_locked": child}


@pytest.mark.asyncio
async def test_cleanup_session_adapter_keeps_lock_after_failed_inflight_creation() -> None:
    lock = asyncio.Lock()
    await lock.acquire()
    parent = _make_adapter(
        _is_session_scoped_adapter=False,
        _session_adapters={},
        _session_adapter_locks={"sess_failed_create": lock},
        _session_adapter_last_used={"sess_failed_create": 1.0},
        _session_adapter_versions={"sess_failed_create": 1},
        _session_adapter_reload_failures={},
    )

    cleanup_task = asyncio.create_task(
        getattr(parent, "cleanup_session_adapter")("sess_failed_create")
    )
    await asyncio.sleep(0)
    assert cleanup_task.done() is False

    lock.release()
    removed = await asyncio.wait_for(cleanup_task, timeout=2)

    assert removed is False
    assert getattr(parent, "_session_adapter_locks")["sess_failed_create"] is lock


@pytest.mark.asyncio
async def test_cleanup_session_adapter_keeps_queued_reconnect_adapter() -> None:
    lock = asyncio.Lock()
    child = _BlockingCleanupChildAdapter()
    replacement = _IdleChildAdapter()
    parent = _make_adapter(
        _is_session_scoped_adapter=False,
        _session_adapters={"sess_reconnect": child},
        _session_adapter_locks={"sess_reconnect": lock},
        _session_adapter_last_used={"sess_reconnect": 1.0},
        _session_adapter_versions={"sess_reconnect": 1},
        _session_adapter_reload_failures={},
    )

    cleanup_task = asyncio.create_task(
        getattr(parent, "cleanup_session_adapter")("sess_reconnect")
    )
    await asyncio.wait_for(child.cleanup_started.wait(), timeout=2)

    async def queued_reconnect() -> None:
        async with lock:
            getattr(parent, "_session_adapters")["sess_reconnect"] = replacement

    reconnect_task = asyncio.create_task(queued_reconnect())
    await asyncio.sleep(0)
    assert reconnect_task.done() is False

    child.cleanup_can_finish.set()
    removed = await asyncio.wait_for(cleanup_task, timeout=2)
    await asyncio.wait_for(reconnect_task, timeout=2)

    assert removed is True
    assert child.cleaned is True
    assert getattr(parent, "_session_adapters") == {"sess_reconnect": replacement}
    assert getattr(parent, "_session_adapter_locks")["sess_reconnect"] is lock


@pytest.mark.asyncio
async def test_idle_eviction_keeps_queued_reconnect_adapter() -> None:
    lock = asyncio.Lock()
    child = _BlockingCleanupChildAdapter()
    replacement = _IdleChildAdapter()
    parent = _make_adapter(
        _is_session_scoped_adapter=False,
        _session_adapters={"sess_idle_reconnect": child},
        _session_adapter_locks={"sess_idle_reconnect": lock},
        _session_adapter_last_used={"sess_idle_reconnect": 1.0},
        _session_adapter_versions={"sess_idle_reconnect": 1},
        _session_adapter_reload_failures={},
        SESSION_ADAPTER_EVICT_BATCH_SIZE=8,
        SESSION_ADAPTER_IDLE_TTL_SEC=1.0,
    )

    eviction_task = asyncio.create_task(
        getattr(parent, "_evict_idle_session_adapters")()
    )
    await asyncio.wait_for(child.cleanup_started.wait(), timeout=2)

    async def queued_reconnect() -> None:
        async with lock:
            getattr(parent, "_session_adapters")["sess_idle_reconnect"] = replacement

    reconnect_task = asyncio.create_task(queued_reconnect())
    await asyncio.sleep(0)
    assert reconnect_task.done() is False

    child.cleanup_can_finish.set()
    await asyncio.wait_for(eviction_task, timeout=2)
    await asyncio.wait_for(reconnect_task, timeout=2)

    assert child.cleaned is True
    assert getattr(parent, "_session_adapters") == {
        "sess_idle_reconnect": replacement
    }
    assert getattr(parent, "_session_adapter_locks")["sess_idle_reconnect"] is lock


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [
    "ok", "no_tool", "missing", "untrusted", "error", "cancelled",
    "active", "approval", "subagent", "locked",
])
async def test_history_reads_evict_expired_adapters_and_keep_protected_sessions(monkeypatch, outcome):
    from openjiuwen.harness.tools.todo_store import TodoSnapshot

    parent = _make_adapter(
        _is_session_scoped_adapter=False,
        _session_adapters={}, _session_adapter_locks={}, _session_adapter_last_used={},
        _session_adapter_versions={}, _session_adapter_reload_failures={},
        _session_instance_config={}, _session_instance_mode="agent", _session_instance_sub_mode=None,
        _config_base_cache=None, SESSION_ADAPTER_IDLE_TTL_SEC=1.0,
        SESSION_ADAPTER_EVICT_BATCH_SIZE=8,
    )
    children = {}

    def create_child(sid, **_kwargs):
        child = _IdleChildAdapter()
        child.create_instance = AsyncMock()
        child.start_interaction = AsyncMock()

        async def read_todos(_sid):
            snapshot = TodoSnapshot([], sid, 1)
            if sid == "history_b":
                if outcome == "error":
                    raise OSError("read failed")
                if outcome == "cancelled":
                    raise asyncio.CancelledError()
                if outcome == "missing":
                    snapshot = None
                elif outcome == "untrusted":
                    snapshot.read_authoritative = False
            return SimpleNamespace(snapshot=snapshot)

        tool = SimpleNamespace(read_todos=read_todos)
        child.get_todo_modify_tool = lambda _sid: None if sid == "history_b" and outcome == "no_tool" else tool
        children[sid] = child
        return child

    parent._new_session_scoped_adapter = create_child
    parent._reload_session_adapter_if_stale = AsyncMock()
    parent._remove_runtime_state_file = Mock()
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.session_ops_service.warmup_session_context", AsyncMock(),
    )
    await parent.read_todo_snapshot("history_a")
    protected = outcome in {"active", "approval", "subagent", "locked"}
    held_lock = None
    if protected:
        await parent.read_todo_snapshot("history_protected")
        parent._session_adapter_last_used["history_protected"] = 0.0
        if outcome == "active":
            children["history_protected"].is_session_active = lambda _sid: True
        elif outcome == "approval":
            parent._sessions_awaiting_tool_permission.add("history_protected")
        elif outcome == "subagent":
            monkeypatch.setattr(
                "jiuwenswarm.server.runtime.agent_adapter.interface_deep._session_has_live_subagent_runtime",
                lambda _adapter, sid: sid == "history_protected",
            )
        else:
            held_lock = parent._session_adapter_locks["history_protected"]
            await held_lock.acquire()
    parent._session_adapter_last_used["history_a"] = 0.0
    try:
        if outcome in {"error", "cancelled"}:
            with pytest.raises(OSError if outcome == "error" else asyncio.CancelledError):
                await parent.read_todo_snapshot("history_b")
        else:
            result = await parent.read_todo_snapshot("history_b")
            assert (result is None) == (outcome in {"no_tool", "missing", "untrusted"})
        assert children["history_a"].cleaned is True
        assert "history_a" not in parent._session_adapters
        assert "history_a" not in parent._session_adapter_locks
        assert "history_b" in parent._session_adapters
        if protected:
            assert children["history_protected"].cleaned is False
            assert "history_protected" in parent._session_adapters
    finally:
        if held_lock is not None:
            held_lock.release()


@pytest.mark.asyncio
async def test_idle_eviction_skips_locked_adapter_without_waiting() -> None:
    lock = asyncio.Lock()
    await lock.acquire()
    child = _IdleChildAdapter()
    parent = _make_adapter(
        _is_session_scoped_adapter=False,
        _session_adapters={"sess_locked_idle": child},
        _session_adapter_locks={"sess_locked_idle": lock},
        _session_adapter_last_used={"sess_locked_idle": 1.0},
        _session_adapter_versions={"sess_locked_idle": 1},
        _session_adapter_reload_failures={},
        SESSION_ADAPTER_EVICT_BATCH_SIZE=8,
        SESSION_ADAPTER_IDLE_TTL_SEC=1.0,
    )

    eviction_task = asyncio.create_task(
        getattr(parent, "_evict_idle_session_adapters")()
    )
    await asyncio.sleep(0)

    assert eviction_task.done() is True
    assert child.cleaned is False
    assert getattr(parent, "_session_adapters") == {"sess_locked_idle": child}
    assert getattr(parent, "_session_adapter_locks")["sess_locked_idle"] is lock

    lock.release()
