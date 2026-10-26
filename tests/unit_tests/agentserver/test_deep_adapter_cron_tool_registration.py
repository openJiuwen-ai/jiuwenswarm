# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for cron tool registration being an init-time, not per-turn, cost."""

from __future__ import annotations

import pytest

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


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


def _make_adapter(language: str = "cn") -> tuple[JiuWenSwarmDeepAdapter, dict[str, int]]:
    """Create a bare adapter whose cron tool build is counted, not executed."""
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._instance = _FakeInstance()
    adapter._cron_tools_registered_language = None
    adapter._is_session_scoped_adapter = True
    adapter._parent_session_id = "sess_a"
    counters = {"build": 0}

    def _build_cron_tools() -> list[_FakeTool]:
        counters["build"] += 1
        return [_FakeTool("cron"), _FakeTool("cron_list_jobs")]

    adapter._build_cron_tools = _build_cron_tools
    adapter._resolve_runtime_language = lambda: adapter._language
    adapter._register_agent_owned_tool = lambda tool, owner_id: None
    adapter._tool_owner_id = lambda: "jiuwenswarm_s_sess_a"
    adapter._language = language
    return adapter, counters


def test_cron_tools_are_built_once_across_turns() -> None:
    """Repeat turns must not rebuild or re-register the cron toolset."""
    adapter, counters = _make_adapter()

    for _ in range(5):
        adapter._ensure_cron_tools_registered("sess_a")

    assert counters["build"] == 1
    assert adapter._instance.ability_manager.add_calls == 2
    assert {card.name for card in adapter._instance.ability_manager.cards} == {
        "cron",
        "cron_list_jobs",
    }


def test_cron_tools_are_not_registered_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The OfficeClaw integration can delegate scheduling to Relay's TaskRunnerV2."""
    monkeypatch.setenv("JIUWENCLAW_DISABLE_CRON_TOOLS", "1")
    adapter, counters = _make_adapter()
    adapter._instance.ability_manager.cards = [
        _FakeCard("cron"),
        _FakeCard("cron_list_jobs"),
        _FakeCard("unrelated_tool"),
    ]

    adapter._ensure_cron_tools_registered("sess_a")

    assert counters["build"] == 0
    assert [card.name for card in adapter._instance.ability_manager.cards] == [
        "unrelated_tool"
    ]
    assert adapter._cron_tools_registered_language is None


@pytest.mark.parametrize("env_value", [None, "0"])
def test_cron_tools_remain_enabled_when_disable_env_is_not_one(
    monkeypatch: pytest.MonkeyPatch,
    env_value: str | None,
) -> None:
    """The native cron tools retain their existing default behavior."""
    if env_value is None:
        monkeypatch.delenv("JIUWENCLAW_DISABLE_CRON_TOOLS", raising=False)
    else:
        monkeypatch.setenv("JIUWENCLAW_DISABLE_CRON_TOOLS", env_value)
    adapter, counters = _make_adapter()

    adapter._ensure_cron_tools_registered("sess_a")

    assert counters["build"] == 1
    assert {card.name for card in adapter._instance.ability_manager.cards} == {
        "cron",
        "cron_list_jobs",
    }


def test_language_change_rebuilds_cron_tools() -> None:
    """Language is baked into the instances, so switching it must rebuild them."""
    adapter, counters = _make_adapter(language="cn")
    adapter._ensure_cron_tools_registered("sess_a")

    adapter._language = "en"
    adapter._ensure_cron_tools_registered("sess_a")

    assert counters["build"] == 2
    # Rebuilt, not accumulated: the previous generation is detached first.
    assert len(adapter._instance.ability_manager.cards) == 2


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
    }


@pytest.mark.parametrize("session_id", ["heartbeat_1", "cron_job_7"])
def test_scheduler_driven_sessions_get_no_cron_tools(session_id: str) -> None:
    """Heartbeat and cron sessions drive the scheduler; they must not carry the tools."""
    adapter, counters = _make_adapter()

    adapter._ensure_cron_tools_registered(session_id)

    assert counters["build"] == 0
    assert adapter._instance.ability_manager.cards == []


def test_empty_build_result_is_not_cached_as_registered() -> None:
    """A backend that yields no tools must stay retryable on the next turn."""
    adapter, counters = _make_adapter()
    adapter._build_cron_tools = lambda: []

    adapter._ensure_cron_tools_registered("sess_a")
    adapter._ensure_cron_tools_registered("sess_a")

    assert adapter._cron_tools_registered_language is None
    assert adapter._instance.ability_manager.cards == []
