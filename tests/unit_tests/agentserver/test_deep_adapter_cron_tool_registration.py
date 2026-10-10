# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for cron tool registration being an init-time, not per-turn, cost."""

from __future__ import annotations

from typing import Any

import pytest

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


@pytest.fixture(autouse=True)
def session_metadata(monkeypatch):
    metadata = {}
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda *args, **kwargs: metadata,
    )
    return metadata


class _FakeCard:
    def __init__(self, name: str) -> None:
        self.name = name
        self.id = name
        self.stateless = False


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.card = _FakeCard(name)


class _FakeAbilityManager:
    """Ability manager double tracking which cards are currently attached."""

    def __init__(self) -> None:
        self.cards: list[_FakeCard] = []
        self.add_calls = 0

    def list(self) -> list[_FakeCard]:
        return list(self.cards)

    def add(self, card: _FakeCard) -> None:
        self.add_calls += 1
        self.cards.append(card)

    def remove(self, name: str) -> None:
        self.cards = [card for card in self.cards if card.name != name]


class _FakeInstance:
    def __init__(self) -> None:
        self.ability_manager = _FakeAbilityManager()


def _make_adapter(language: str = "cn") -> tuple[JiuWenSwarmDeepAdapter, dict[str, Any]]:
    """Create a bare adapter whose cron tool build is counted, not executed."""
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._instance = _FakeInstance()
    adapter._cron_tools_registered_language = None
    adapter._cron_tools_registered_allow_update = None
    adapter._is_session_scoped_adapter = True
    adapter._parent_session_id = "sess_a"
    counters: dict[str, Any] = {"build": 0, "allow_create": [], "allow_update": []}

    def _build_cron_tools(
        *, allow_create: bool = True, allow_update: bool = True
    ) -> list[_FakeTool]:
        counters["build"] += 1
        counters["allow_create"] = [*counters["allow_create"], allow_create]
        counters["allow_update"] = [*counters["allow_update"], allow_update]
        tools = [_FakeTool("cron"), _FakeTool("cron_list_jobs")]
        if allow_create:
            tools.append(_FakeTool("cron_create_job"))
        if allow_update:
            tools.append(_FakeTool("cron_update_job"))
        return tools

    adapter._build_cron_tools = _build_cron_tools
    adapter._resolve_runtime_language = lambda: adapter._language
    adapter._register_agent_owned_tool = lambda tool, owner_id: None
    adapter._tool_owner_id = lambda: "jiuwenswarm_s_sess_a"
    adapter._language = language
    return adapter, counters


def _make_heartbeat_adapter(
    session_id: str, *, is_cron_execution: bool = False
) -> JiuWenSwarmDeepAdapter:
    """Bare adapter with just enough state for the heartbeat rail gate."""
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._heartbeat_service = object()
    adapter._is_cron_execution = is_cron_execution
    adapter._parent_session_id = session_id
    adapter._runtime_cron_tool_context = object()
    return adapter


def test_cron_tools_are_built_once_across_turns() -> None:
    """Repeat turns must not rebuild or re-register the cron toolset."""
    adapter, counters = _make_adapter()

    for _ in range(5):
        adapter._ensure_cron_tools_registered("sess_a")

    assert counters["build"] == 1
    assert counters["allow_create"] == [True]
    assert counters["allow_update"] == [True]
    assert adapter._instance.ability_manager.add_calls == 4
    assert {card.name for card in adapter._instance.ability_manager.cards} == {
        "cron",
        "cron_list_jobs",
        "cron_create_job",
        "cron_update_job",
    }


def test_language_change_rebuilds_cron_tools() -> None:
    """Language is baked into the instances, so switching it must rebuild them."""
    adapter, counters = _make_adapter(language="cn")
    adapter._ensure_cron_tools_registered("sess_a")

    adapter._language = "en"
    adapter._ensure_cron_tools_registered("sess_a")

    assert counters["build"] == 2
    # Rebuilt, not accumulated: the previous generation is detached first.
    assert len(adapter._instance.ability_manager.cards) == 4


def test_agent_rebuild_reregisters_cron_tools() -> None:
    """A rebuilt agent gets a fresh AbilityManager and must be re-populated.

    The language fingerprint alone would still read as "registered" here, which
    would drop the cron tools for the life of the adapter.
    """
    adapter, counters = _make_adapter()
    adapter._ensure_cron_tools_registered("sess_a")

    adapter._instance = _FakeInstance()
    adapter._ensure_cron_tools_registered("sess_a")

    assert counters["build"] == 2
    assert {card.name for card in adapter._instance.ability_manager.cards} == {
        "cron",
        "cron_list_jobs",
        "cron_create_job",
        "cron_update_job",
    }


@pytest.mark.parametrize("session_id", ["heartbeat_1", "cron_job_7"])
def test_scheduler_driven_sessions_get_no_cron_tools(session_id: str) -> None:
    """Heartbeat and cron sessions drive the scheduler; they must not carry the tools."""
    adapter, counters = _make_adapter()

    adapter._ensure_cron_tools_registered(session_id)

    assert counters["build"] == 0
    assert adapter._instance.ability_manager.cards == []


@pytest.mark.parametrize("session_id", ["heartbeat_1", "cron_job_7"])
def test_scheduler_driven_session_removes_registered_cron_tools(session_id: str) -> None:
    """Scheduler-driven sessions must also detach tools registered earlier."""
    adapter, _counters = _make_adapter()
    adapter._ensure_cron_tools_registered("sess_a")
    adapter._instance.ability_manager.add(_FakeCard("unrelated_tool"))

    adapter._ensure_cron_tools_registered(session_id)

    assert [card.name for card in adapter._instance.ability_manager.cards] == ["unrelated_tool"]
    assert adapter._cron_tools_registered_language is None
    assert adapter._cron_tools_registered_allow_create is None
    assert adapter._cron_tools_registered_allow_update is None


def test_empty_build_result_is_not_cached_as_registered() -> None:
    """A backend that yields no tools must stay retryable on the next turn."""
    adapter, counters = _make_adapter()
    adapter._build_cron_tools = lambda **kwargs: []

    adapter._ensure_cron_tools_registered("sess_a")
    adapter._ensure_cron_tools_registered("sess_a")

    assert adapter._cron_tools_registered_language is None
    assert adapter._instance.ability_manager.cards == []


@pytest.mark.parametrize("already_registered", [False, True])
def test_cron_metadata_session_removes_all_cron_tools(
    session_metadata, already_registered,
) -> None:
    """cron 执行会话（持久化 cron_id）全面不暴露 cron 工具。

    已按完整工具集注册过的会话，后续识别出 cron 来源时也要把已注册的
    cron 工具整体摘除（含 list/get 等只读管理工具）。
    """
    adapter, counters = _make_adapter()
    if already_registered:
        adapter._ensure_cron_tools_registered("sess_a")
    adapter._instance.ability_manager.add(_FakeCard("unrelated_tool"))
    session_metadata["cron_id"] = "parent-job"

    adapter._ensure_cron_tools_registered("sess_a")

    assert counters["build"] == int(already_registered)
    assert [card.name for card in adapter._instance.ability_manager.cards] == [
        "unrelated_tool"
    ]
    assert adapter._cron_tools_registered_language is None
    assert adapter._cron_tools_registered_allow_create is None
    assert adapter._cron_tools_registered_allow_update is None


def test_underscore_cron_prefix_session_gets_no_cron_tools(session_metadata) -> None:
    """老链路 ``__cron__`` 前缀的执行会话，即使元数据缺失也完全不暴露 cron 工具。"""
    adapter, counters = _make_adapter()

    for _ in range(3):
        adapter._ensure_cron_tools_registered("__cron___18f_abc123def456")

    assert counters["build"] == 0
    assert adapter._instance.ability_manager.cards == []


def test_heartbeat_rail_skipped_for_cron_execution_sessions(session_metadata) -> None:
    """cron 执行会话不挂心跳工具：渠道/会话前缀/持久化 cron_id 三种来源都命中。"""
    cases = [
        # 单 Agent 链路的内部执行渠道（channel_id == "__cron__"）。
        {"session_id": "sess_a", "is_cron_execution": True},
        # 老链路 __cron__ 前缀会话。
        {"session_id": "__cron___18f_abc123def456", "is_cron_execution": False},
        # 新链路 cron_ 前缀执行会话。
        {"session_id": "cron_1930_job1", "is_cron_execution": False},
    ]
    for case in cases:
        adapter = _make_heartbeat_adapter(**case)
        assert adapter._build_heartbeat_rail() is None, case

    # 持久化 cron_id 元数据来源。
    session_metadata["cron_id"] = "parent-job"
    adapter = _make_heartbeat_adapter("sess_a")
    assert adapter._build_heartbeat_rail() is None


def test_heartbeat_rail_mounted_for_non_cron_sessions(session_metadata) -> None:
    """普通会话与 heartbeat/health_check 调度器会话仍正常挂载心跳工具。"""
    from jiuwenswarm.agents.harness.code.rails.heartbeat_rail import HeartbeatRail

    for session_id in ("sess_a", "heartbeat_1", "health_check_2"):
        adapter = _make_heartbeat_adapter(session_id)
        rail = adapter._build_heartbeat_rail()
        assert isinstance(rail, HeartbeatRail), session_id
