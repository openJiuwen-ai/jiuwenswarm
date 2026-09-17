# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for how adapter tool registrations are scoped and reclaimed."""

from __future__ import annotations

import pytest
from openjiuwen.core.foundation.tool import ToolCard

from jiuwenswarm.common import tool_ownership
from jiuwenswarm.common.tool_ownership import (
    mark_stateless,
    qualify_tool_id,
    register_tool,
    unregister_tool,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    _AGENT_CARD_ID,
    JiuWenSwarmDeepAdapter,
)


class _FakeTool:
    """Minimal tool exposing the single attribute the registration path reads."""

    def __init__(self, name: str, tool_id: str | None = None, stateless: bool = False) -> None:
        self.card = ToolCard(id=tool_id or name, name=name, description=name)
        self.card.stateless = stateless


class _FakeResourceMgr:
    """Registry double recording adds by id along with their registration flags."""

    def __init__(self) -> None:
        self.tools: dict[str, _FakeTool] = {}
        self.adds: list[tuple[str, bool, bool]] = []
        self.removed: list[str] = []

    def add_tool(
        self,
        tool: _FakeTool,
        *,
        tag: object | None = None,
        refresh: bool = False,
        skip_if_exists: bool = False,
    ) -> None:
        self.adds.append((tool.card.id, refresh, skip_if_exists))
        if skip_if_exists and tool.card.id in self.tools:
            return
        self.tools[tool.card.id] = tool

    def remove_tool(self, tool_id: str) -> None:
        self.removed.append(tool_id)
        self.tools.pop(tool_id, None)


@pytest.fixture(name="resource_mgr")
def _resource_mgr(monkeypatch: pytest.MonkeyPatch) -> _FakeResourceMgr:
    fake = _FakeResourceMgr()
    monkeypatch.setattr(tool_ownership.Runner, "resource_mgr", fake)
    return fake


def _make_adapter(session_id: str | None) -> JiuWenSwarmDeepAdapter:
    """Create a bare adapter carrying only the state the owner id derives from."""
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._is_session_scoped_adapter = session_id is not None
    adapter._parent_session_id = session_id
    return adapter


def test_agent_owned_tool_is_qualified_by_owner(resource_mgr: _FakeResourceMgr) -> None:
    """A stateful tool registers under an owner-qualified id with refresh."""
    tool = _FakeTool("free_search")

    register_tool(tool, "jiuwenswarm_sess_a")

    assert tool.card.id == "free_search_jiuwenswarm_sess_a"
    assert resource_mgr.adds == [("free_search_jiuwenswarm_sess_a", True, False)]


def test_shared_tool_keeps_bare_id_and_first_instance(resource_mgr: _FakeResourceMgr) -> None:
    """A stateless tool keeps its bare id and a repeat add is a no-op."""
    first = _FakeTool("video_understanding")
    second = _FakeTool("video_understanding")
    mark_stateless([first, second])

    register_tool(first, "jiuwenswarm_sess_a")
    register_tool(second, "jiuwenswarm_sess_b")

    assert first.card.id == "video_understanding"
    assert second.card.id == "video_understanding"
    assert resource_mgr.adds == [
        ("video_understanding", False, True),
        ("video_understanding", False, True),
    ]
    assert resource_mgr.tools["video_understanding"] is first


def test_missing_owner_degrades_to_shared(resource_mgr: _FakeResourceMgr) -> None:
    """With no owner to attribute it to, a tool must not be id-qualified."""
    tool = _FakeTool("wiki_query")

    register_tool(tool, None)

    assert tool.card.id == "wiki_query"
    assert resource_mgr.adds == [("wiki_query", False, True)]


def test_requalifying_the_same_tool_is_idempotent(resource_mgr: _FakeResourceMgr) -> None:
    """Re-registering an already-qualified card must not stack owner suffixes."""
    tool = _FakeTool("paid_search")

    register_tool(tool, "jiuwenswarm_root")
    register_tool(tool, "jiuwenswarm_root")

    assert tool.card.id == "paid_search_jiuwenswarm_root"


def test_unregister_leaves_shared_tools_registered(resource_mgr: _FakeResourceMgr) -> None:
    """Dropping a shared tool must not pull it out from under other agents."""
    owned = _FakeTool("image_ocr")
    shared = mark_stateless([_FakeTool("generate_image")])[0]
    register_tool(owned, "jiuwenswarm_sess_a")
    register_tool(shared, "jiuwenswarm_sess_a")

    unregister_tool(owned)
    unregister_tool(shared)

    assert resource_mgr.removed == ["image_ocr_jiuwenswarm_sess_a"]
    assert "generate_image" in resource_mgr.tools


def test_concurrent_sessions_do_not_share_owner_ids() -> None:
    """Two session adapters must register their instances under distinct ids."""
    first = _make_adapter("sess_a")
    second = _make_adapter("sess_b")

    assert first._tool_owner_id() != second._tool_owner_id()
    assert first._tool_owner_id().startswith(f"{_AGENT_CARD_ID}_")
    assert second._tool_owner_id().startswith(f"{_AGENT_CARD_ID}_")


def test_root_owner_id_is_distinct_from_any_session() -> None:
    """The root adapter owns a scope of its own, never a session's."""
    root = _make_adapter(None)
    scoped = _make_adapter("root")

    assert root._tool_owner_id() == f"{_AGENT_CARD_ID}_root"
    assert scoped._tool_owner_id() != root._tool_owner_id()


def test_owner_id_matches_the_teardown_contract(resource_mgr: _FakeResourceMgr) -> None:
    """Registered ids must be exactly what ``AbilityManager.teardown_tools`` looks for.

    Teardown reclaims a card only when ``card.id == f"{name}_{owner_id}"``; if
    registration ever diverged from that shape the instance would leak for the
    life of the process.
    """
    adapter = _make_adapter("sess_a")
    owner_id = adapter._tool_owner_id()
    tool = _FakeTool("free_search")

    adapter._register_agent_owned_tool(tool, owner_id)

    assert tool.card.id == qualify_tool_id(tool.card, owner_id)
    assert tool.card.id == f"{tool.card.name}_{owner_id}"
    assert tool.card.stateless is False


def test_shared_declaration_on_the_card_wins_over_the_call_site(
    resource_mgr: _FakeResourceMgr,
) -> None:
    """A card declared shared stays shared even via the agent-owned entry point.

    The two must agree: qualifying a shared card would register an id that
    teardown (which skips stateless cards) never reclaims.
    """
    adapter = _make_adapter("sess_a")
    tool = mark_stateless([_FakeTool("user_todos")])[0]

    adapter._register_agent_owned_tool(tool, adapter._tool_owner_id())

    assert tool.card.id == "user_todos"
    assert resource_mgr.adds == [("user_todos", False, True)]
