# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for session-adapter registry/callback cleanup (issue#5240 / !8190).

Local venvs may lag ``agent_gateway`` / ``openjiuwen`` pins that develop
``interface_deep`` imports at module load. Stub the thin gateway re-export
surface before importing the adapter so these lifecycle tests stay runnable
without a full workspace sync.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def _patch_agent_gateway_skill_source_exports() -> None:
    """Fill names develop re-exports when the installed gateway pin is older."""
    import agent_gateway.extensions.sdk.skill_source as skill_source

    names = (
        "SKILL_CAPABILITIES",
        "ArtifactDescriptor",
        "DownloadPolicy",
        "InstalledArtifact",
        "ProviderInvocationContext",
        "SkillArtifactVerificationError",
        "SkillCandidate",
        "SkillCapabilityError",
        "SkillDetail",
        "SkillDownloadError",
        "SkillRef",
        "SkillSearchRequest",
        "SkillSearchResult",
        "SkillSourceExtension",
        "SkillSourceProvider",
        "SkillUpdateStatus",
        "SkillVerificationResult",
        "SourceConfig",
        "SourceDescriptor",
    )
    for name in names:
        if hasattr(skill_source, name):
            continue
        setattr(skill_source, name, () if name == "SKILL_CAPABILITIES" else object())


_patch_agent_gateway_skill_source_exports()

from jiuwenswarm.server.runtime.agent_adapter import interface_deep  # noqa: E402
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (  # noqa: E402
    JiuWenSwarmDeepAdapter,
)


class _FakeCard:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeResourceMgr:
    def __init__(self) -> None:
        self._id_to_card: dict[str, _FakeCard] = {}
        self.removed: list[str] = []

    def remove_tool(self, tool_id: str) -> None:
        self.removed.append(tool_id)
        self._id_to_card.pop(tool_id, None)


def _make_adapter(*, session_id: str | None, instance: object | None = None) -> JiuWenSwarmDeepAdapter:
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._is_session_scoped_adapter = session_id is not None
    adapter._parent_session_id = session_id
    adapter._instance = instance
    return adapter


def test_purge_removes_only_exact_owner_qualified_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Purge must drop ``{name}_{owner}`` for this session and leave others."""
    adapter = _make_adapter(session_id="sess_a")
    owner = adapter._tool_owner_id()
    other_owner = _make_adapter(session_id="sess_b")._tool_owner_id()

    rm = _FakeResourceMgr()
    own_id = f"free_search_{owner}"
    other_id = f"free_search_{other_owner}"
    shared_id = "video_understanding"
    # Suffix collision: another id ends with this owner segment but is not
    # ``{name}_{owner}`` for a real card name — must not be removed.
    decoy_id = f"not_a_tool_name_prefix_{owner}"
    rm._id_to_card = {
        own_id: _FakeCard("free_search"),
        other_id: _FakeCard("free_search"),
        shared_id: _FakeCard("video_understanding"),
        decoy_id: _FakeCard("decoy"),
    }
    monkeypatch.setattr(interface_deep.Runner, "resource_mgr", rm)

    adapter._purge_session_scoped_registry_entries()

    assert own_id in rm.removed
    assert other_id not in rm.removed
    assert shared_id not in rm.removed
    assert decoy_id not in rm.removed
    assert own_id not in rm._id_to_card
    assert other_id in rm._id_to_card
    assert shared_id in rm._id_to_card


def test_purge_is_noop_for_root_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    root = _make_adapter(session_id=None)
    rm = _FakeResourceMgr()
    owner = "jiuwenswarm_s_sess_a"
    rid = f"free_search_{owner}"
    rm._id_to_card = {rid: _FakeCard("free_search")}
    monkeypatch.setattr(interface_deep.Runner, "resource_mgr", rm)

    root._purge_session_scoped_registry_entries()

    assert rm.removed == []
    assert rid in rm._id_to_card


@pytest.mark.asyncio
async def test_release_callbacks_prefers_aclose_then_dfs_clear() -> None:
    """aclose runs first; DFS still clears managers reachable from the instance."""
    own_manager = SimpleNamespace(clear=AsyncMock())
    nested = SimpleNamespace(_agent_callback_manager=own_manager)
    instance = SimpleNamespace(
        aclose=AsyncMock(),
        _agent_callback_manager=own_manager,
        nested=nested,
    )
    adapter = _make_adapter(session_id="sess_a", instance=instance)

    await adapter._release_session_agent_callbacks()

    instance.aclose.assert_awaited_once()
    assert own_manager.clear.await_count == 1


@pytest.mark.asyncio
async def test_release_callbacks_prefers_public_agent_callback_manager() -> None:
    """Public property wins; private-only nested managers still clear."""
    public_manager = SimpleNamespace(clear=AsyncMock())
    private_manager = SimpleNamespace(clear=AsyncMock())

    class _PublicOnly:
        @property
        def agent_callback_manager(self):
            return public_manager

    nested = SimpleNamespace(_agent_callback_manager=private_manager)
    instance = SimpleNamespace(
        agent=_PublicOnly(),
        nested=nested,
    )
    adapter = _make_adapter(session_id="sess_a", instance=instance)

    await adapter._release_session_agent_callbacks()

    public_manager.clear.assert_awaited_once()
    private_manager.clear.assert_awaited_once()


@pytest.mark.asyncio
async def test_release_callbacks_clears_manager_reached_before_owner() -> None:
    """Manager must still clear when a sideways ref is traversed first.

    Traversal ``seen`` must not double as manager-collection dedup: if the
    manager object is visited as a plain graph node before its owning agent,
    collection via ``manager_ids`` must still enqueue ``clear()``.
    """
    nested_manager = SimpleNamespace(clear=AsyncMock())
    nested_agent = SimpleNamespace(_agent_callback_manager=nested_manager)
    # Put the manager ahead of the nested agent in ``vars`` insertion order so
    # DFS pops the manager object before visiting the owner.
    instance = SimpleNamespace()
    instance.sideways = nested_manager
    instance.nested = nested_agent
    adapter = _make_adapter(session_id="sess_a", instance=instance)

    await adapter._release_session_agent_callbacks()

    nested_manager.clear.assert_awaited_once()


@pytest.mark.asyncio
async def test_release_callbacks_dfs_skips_adapter_back_edges() -> None:
    """DFS must not follow adapter references into other sessions' managers."""
    other_manager = SimpleNamespace(clear=AsyncMock())
    other_adapter = _make_adapter(
        session_id="sess_b",
        instance=SimpleNamespace(_agent_callback_manager=other_manager),
    )
    own_manager = SimpleNamespace(clear=AsyncMock())
    instance = SimpleNamespace(
        _agent_callback_manager=own_manager,
        # Skill-turbo style back edge to another live session adapter.
        skill_turbo_adapter=other_adapter,
    )
    adapter = _make_adapter(session_id="sess_a", instance=instance)

    await adapter._release_session_agent_callbacks()

    own_manager.clear.assert_awaited_once()
    other_manager.clear.assert_not_awaited()


@pytest.mark.asyncio
async def test_release_callbacks_falls_back_to_dfs_without_aclose() -> None:
    """Old cores without aclose still clear via DFS."""
    manager = SimpleNamespace(clear=AsyncMock())
    instance = SimpleNamespace(_agent_callback_manager=manager)
    adapter = _make_adapter(session_id="sess_a", instance=instance)

    await adapter._release_session_agent_callbacks()

    manager.clear.assert_awaited_once()


def test_teardown_calls_purge_after_ability_teardown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """teardown_tools path must also run the session registry purge."""
    purged: list[str] = []
    ability = SimpleNamespace(teardown_tools=lambda: None)
    adapter = _make_adapter(
        session_id="sess_a",
        instance=SimpleNamespace(ability_manager=ability),
    )
    monkeypatch.setattr(
        adapter,
        "_purge_session_scoped_registry_entries",
        lambda: purged.append("purged"),
    )

    adapter._teardown_agent_owned_tools()

    assert purged == ["purged"]


def test_teardown_purges_when_instance_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tools-registered-before-instance-ready must still purge registry ids."""
    purged: list[str] = []
    adapter = _make_adapter(session_id="sess_a", instance=None)
    monkeypatch.setattr(
        adapter,
        "_purge_session_scoped_registry_entries",
        lambda: purged.append("purged"),
    )

    adapter._teardown_agent_owned_tools()

    assert purged == ["purged"]


def test_teardown_purges_when_ability_manager_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    purged: list[str] = []
    adapter = _make_adapter(
        session_id="sess_a",
        instance=SimpleNamespace(),  # no ability_manager
    )
    monkeypatch.setattr(
        adapter,
        "_purge_session_scoped_registry_entries",
        lambda: purged.append("purged"),
    )

    adapter._teardown_agent_owned_tools()

    assert purged == ["purged"]
