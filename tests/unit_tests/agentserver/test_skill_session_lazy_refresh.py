# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for per-session SkillUseRail lazy refresh after install/uninstall."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from jiuwenswarm.server.runtime.agent_adapter import interface_deep as interface_deep_module
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


def _make_root(**overrides: object) -> JiuWenSwarmDeepAdapter:
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._is_session_scoped_adapter = False
    adapter._session_parent_adapter = None
    adapter._agent_id = "bot-prebuilt"
    adapter._service_id = "svc-prebuilt"
    adapter._skill_enabled_version = 0
    adapter._session_skill_versions = {}
    adapter._session_skill_refresh_failures = {}
    adapter._session_adapters = {}
    for name, value in overrides.items():
        setattr(adapter, name, value)
    return adapter


def _make_session_child(
    parent: JiuWenSwarmDeepAdapter,
    *,
    session_id: str = "sess_a",
) -> JiuWenSwarmDeepAdapter:
    child = object.__new__(JiuWenSwarmDeepAdapter)
    child._is_session_scoped_adapter = True
    child._session_parent_adapter = parent
    child._parent_session_id = session_id
    child._agent_id = parent._agent_id
    child._service_id = parent._service_id
    child._skill_enabled_version = 0
    child._session_skill_versions = {}
    child._session_skill_refresh_failures = {}
    child._session_adapters = {}
    return child


@pytest.mark.asyncio
async def test_on_installed_skills_changed_bumps_root_after_successful_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In-session SkillToolkit notify must bump so peer sessions catch up lazily."""
    root = _make_root(_skill_enabled_version=2)
    monkeypatch.setattr(interface_deep_module, "is_skill_prebuilt_tenant", lambda *_a, **_k: True)
    monkeypatch.setattr(interface_deep_module, "_skill_session_lazy_refresh_enabled", lambda: True)

    async def _refresh_ok() -> bool:
        return True

    root.refresh_enabled_skills_from_db = _refresh_ok  # type: ignore[method-assign]

    await root._on_installed_skills_changed()

    assert root._skill_enabled_version == 3


@pytest.mark.asyncio
async def test_on_installed_skills_changed_bumps_even_when_refresh_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ledger already changed; a failed local refresh must not drop the bump."""
    root = _make_root(_skill_enabled_version=2)
    monkeypatch.setattr(interface_deep_module, "is_skill_prebuilt_tenant", lambda *_a, **_k: True)
    monkeypatch.setattr(interface_deep_module, "_skill_session_lazy_refresh_enabled", lambda: True)

    async def _refresh_fail() -> bool:
        return False

    root.refresh_enabled_skills_from_db = _refresh_fail  # type: ignore[method-assign]

    await root._on_installed_skills_changed()

    assert root._skill_enabled_version == 3


@pytest.mark.asyncio
async def test_session_child_on_installed_skills_changed_forwards_bump_and_marks_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Session toolkit: bump parent first, refresh, stamp initiator with bump version."""
    root = _make_root(_skill_enabled_version=5)
    child = _make_session_child(root, session_id="sess_a")
    monkeypatch.setattr(interface_deep_module, "is_skill_prebuilt_tenant", lambda *_a, **_k: True)
    monkeypatch.setattr(interface_deep_module, "_skill_session_lazy_refresh_enabled", lambda: True)

    child_refreshed = False

    async def _child_refresh() -> bool:
        nonlocal child_refreshed
        child_refreshed = True
        return True

    child.refresh_enabled_skills_from_db = _child_refresh  # type: ignore[method-assign]

    await child._on_installed_skills_changed()

    assert child_refreshed is True
    assert root._skill_enabled_version == 6
    assert root._session_skill_versions.get("sess_a") == 6


@pytest.mark.asyncio
async def test_session_child_refresh_failure_still_bumps_but_leaves_session_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed initiator refresh keeps the session stale so the next turn retries."""
    root = _make_root(_skill_enabled_version=5)
    child = _make_session_child(root, session_id="sess_a")
    monkeypatch.setattr(interface_deep_module, "is_skill_prebuilt_tenant", lambda *_a, **_k: True)
    monkeypatch.setattr(interface_deep_module, "_skill_session_lazy_refresh_enabled", lambda: True)

    async def _child_refresh() -> bool:
        return False

    child.refresh_enabled_skills_from_db = _child_refresh  # type: ignore[method-assign]

    await child._on_installed_skills_changed()

    assert root._skill_enabled_version == 6
    assert "sess_a" not in root._session_skill_versions


@pytest.mark.asyncio
async def test_session_child_marks_bump_version_not_root_after_concurrent_bump(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent bump during refresh must not sticky-mark the initiator current."""
    root = _make_root(_skill_enabled_version=5)
    child = _make_session_child(root, session_id="sess_a")
    monkeypatch.setattr(interface_deep_module, "is_skill_prebuilt_tenant", lambda *_a, **_k: True)
    monkeypatch.setattr(interface_deep_module, "_skill_session_lazy_refresh_enabled", lambda: True)

    gate = asyncio.Event()

    async def _child_refresh() -> bool:
        # Simulate another install completing (and bumping) while we await.
        root.bump_skill_enabled_version()
        gate.set()
        return True

    child.refresh_enabled_skills_from_db = _child_refresh  # type: ignore[method-assign]

    await child._on_installed_skills_changed()
    assert gate.is_set()
    assert root._skill_enabled_version == 7  # this notify + concurrent bump
    # Stamp the version claimed before refresh (6), not the post-await root (7).
    assert root._session_skill_versions.get("sess_a") == 6


def _make_push_root(**overrides: object) -> JiuWenSwarmDeepAdapter:
    root = _make_root(_session_adapter_locks={}, **overrides)
    return root


@pytest.mark.asyncio
async def test_push_refreshes_bound_session_same_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Web install bound to a live session refreshes it now, not next turn."""
    root = _make_push_root(_skill_enabled_version=2)
    child = _make_session_child(root, session_id="sess_a")
    root._session_adapters["sess_a"] = child
    monkeypatch.setattr(interface_deep_module, "is_skill_prebuilt_tenant", lambda *_a, **_k: True)
    monkeypatch.setattr(interface_deep_module, "_skill_session_lazy_refresh_enabled", lambda: True)

    child_refreshed = False

    async def _child_refresh() -> bool:
        nonlocal child_refreshed
        child_refreshed = True
        return True

    child.refresh_enabled_skills_from_db = _child_refresh  # type: ignore[method-assign]

    await root._push_skill_refresh_to_bound_session(SimpleNamespace(session_id="sess_a"))

    assert child_refreshed is True
    # Same lazy bookkeeping path: stamped current at the root stamp (2).
    assert root._session_skill_versions.get("sess_a") == 2
    # Live session: the lock entry stays for subsequent turns.
    assert "sess_a" in root._session_adapter_locks


@pytest.mark.asyncio
async def test_push_ignores_session_without_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Synthetic facade sessions (bff-*) never map to an adapter here: no push."""
    root = _make_push_root(_skill_enabled_version=2)
    monkeypatch.setattr(interface_deep_module, "is_skill_prebuilt_tenant", lambda *_a, **_k: True)
    monkeypatch.setattr(interface_deep_module, "_skill_session_lazy_refresh_enabled", lambda: True)

    refreshed = False

    async def _refresh() -> bool:
        nonlocal refreshed
        refreshed = True
        return True

    root.refresh_enabled_skills_from_db = _refresh  # type: ignore[method-assign]

    await root._push_skill_refresh_to_bound_session(SimpleNamespace(session_id="bff-656b24ae"))

    assert refreshed is False
    assert root._session_adapters == {}


@pytest.mark.asyncio
async def test_push_without_session_id_is_noop() -> None:
    """No session binding on the request: keep the lazy semantics."""
    root = _make_push_root(_skill_enabled_version=2)

    await root._push_skill_refresh_to_bound_session(SimpleNamespace(session_id=None))
    await root._push_skill_refresh_to_bound_session(None)

    assert root._session_skill_versions == {}


@pytest.mark.asyncio
async def test_push_refresh_failure_falls_back_to_lazy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed push must neither raise nor mark the session current."""
    root = _make_push_root(_skill_enabled_version=2)
    child = _make_session_child(root, session_id="sess_a")
    root._session_adapters["sess_a"] = child
    monkeypatch.setattr(interface_deep_module, "is_skill_prebuilt_tenant", lambda *_a, **_k: True)
    monkeypatch.setattr(interface_deep_module, "_skill_session_lazy_refresh_enabled", lambda: True)

    async def _child_refresh() -> bool:
        return False

    child.refresh_enabled_skills_from_db = _child_refresh  # type: ignore[method-assign]

    await root._push_skill_refresh_to_bound_session(SimpleNamespace(session_id="sess_a"))

    # Stays stale: next turn boundary retries (lazy semantics preserved).
    assert "sess_a" not in root._session_skill_versions


@pytest.mark.asyncio
async def test_push_refresh_raise_is_swallowed_and_records_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raising refresh must not escape the push; it records a failure stamp.

    Pins the exception-safety that makes flag-based (not raise-based)
    sentinels mandatory in the eviction test: the refresh chain swallows
    exceptions by design — warning + failure stamp + shared 30s retry
    suppression — and falls back to the lazy next-turn semantics.
    """
    root = _make_push_root(_skill_enabled_version=2)
    child = _make_session_child(root, session_id="sess_a")
    root._session_adapters["sess_a"] = child
    monkeypatch.setattr(interface_deep_module, "is_skill_prebuilt_tenant", lambda *_a, **_k: True)
    monkeypatch.setattr(interface_deep_module, "_skill_session_lazy_refresh_enabled", lambda: True)

    async def _child_refresh() -> bool:
        raise RuntimeError("db unavailable")

    child.refresh_enabled_skills_from_db = _child_refresh  # type: ignore[method-assign]

    await root._push_skill_refresh_to_bound_session(SimpleNamespace(session_id="sess_a"))

    # Swallowed: no raise escapes, session stays stale for the next boundary.
    assert "sess_a" not in root._session_skill_versions
    failure = root._session_skill_refresh_failures.get("sess_a")
    assert failure is not None and failure[0] == 2


@pytest.mark.asyncio
async def test_push_survives_adapter_evicted_while_waiting_on_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adapter evicted by a lock holder while the push queues: no error.

    Mirrors the turn-boundary path holding the session lock (``setdefault``
    shares the same lock) and evicting the adapter between the push's guard
    check and its lock acquisition.
    """
    root = _make_push_root(_skill_enabled_version=2)
    child = _make_session_child(root, session_id="sess_a")
    root._session_adapters["sess_a"] = child
    monkeypatch.setattr(interface_deep_module, "is_skill_prebuilt_tenant", lambda *_a, **_k: True)
    monkeypatch.setattr(interface_deep_module, "_skill_session_lazy_refresh_enabled", lambda: True)

    lock = asyncio.Lock()
    root._session_adapter_locks["sess_a"] = lock
    lock_held = asyncio.Event()
    release_lock = asyncio.Event()

    async def _evict_holder() -> None:
        async with lock:
            lock_held.set()
            await release_lock.wait()
            root._session_adapters.pop("sess_a", None)

    holder = asyncio.create_task(_evict_holder())
    await lock_held.wait()

    refresh_ran = False

    async def _child_refresh() -> bool:
        # Flag, not a raise: the refresh chain swallows exceptions by design
        # (``_refresh_session_skills_if_stale`` logs and records a failure
        # stamp instead of propagating), so a raising sentinel would be
        # swallowed and this regression would pass silently.
        nonlocal refresh_ran
        refresh_ran = True
        return False

    child.refresh_enabled_skills_from_db = _child_refresh  # type: ignore[method-assign]

    push_task = asyncio.create_task(
        root._push_skill_refresh_to_bound_session(SimpleNamespace(session_id="sess_a"))
    )
    await asyncio.sleep(0)  # push passes its guard and queues on the lock
    release_lock.set()

    await push_task
    await holder

    assert refresh_ran is False
    # No refresh was even attempted: no version stamp, no failure stamp.
    assert root._session_skill_versions == {}
    assert root._session_skill_refresh_failures == {}
    # The orphaned lock entry is dropped: has_session_runtime must not keep
    # reporting runtime for a session cleaned up while the push waited.
    assert "sess_a" not in root._session_adapter_locks


@pytest.mark.asyncio
async def test_refresh_enabled_skills_from_db_serializes_concurrent_swaps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent refresh entry points must not interleave the rail swap.

    The push path (root session lock held), the turn-boundary refresh and the
    in-session ``_on_installed_skills_changed`` callback can all call
    ``refresh_enabled_skills_from_db`` on one adapter concurrently; the swap
    section has await points, so the public entry serializes the whole body.
    """
    root = _make_push_root(_instance=object(), _skill_refresh_lock=asyncio.Lock())
    monkeypatch.setattr(interface_deep_module, "is_skill_prebuilt_tenant", lambda *_a, **_k: True)

    in_flight = 0
    max_in_flight = 0

    async def _locked_body() -> bool:
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        try:
            await asyncio.sleep(0)  # yield so the peer task can queue on the lock
            return True
        finally:
            in_flight -= 1

    root._refresh_enabled_skills_from_db_locked = _locked_body  # type: ignore[method-assign]

    results = await asyncio.gather(
        root.refresh_enabled_skills_from_db(),
        root.refresh_enabled_skills_from_db(),
    )

    assert results == [True, True]
    assert max_in_flight == 1
