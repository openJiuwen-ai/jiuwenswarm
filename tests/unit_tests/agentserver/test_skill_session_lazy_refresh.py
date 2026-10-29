# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for per-session SkillUseRail lazy refresh after install/uninstall."""

from __future__ import annotations

import asyncio

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
