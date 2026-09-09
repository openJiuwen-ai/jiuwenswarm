from __future__ import annotations

import asyncio
from collections.abc import Iterator
from types import SimpleNamespace

import pytest

from jiuwenswarm.server.runtime.session.kv_cache import kv_cache_product_hooks


@pytest.fixture(autouse=True)
def _clear_product_state() -> Iterator[None]:
    from jiuwenswarm.server.runtime.session.kv_cache.kv_cache_task_guard import (
        get_session_kv_cache_task_guard,
    )

    get_session_kv_cache_task_guard().clear()
    kv_cache_product_hooks._PRODUCT_GUARD_TASKS.clear()
    yield
    get_session_kv_cache_task_guard().clear()
    kv_cache_product_hooks._PRODUCT_GUARD_TASKS.clear()


def _set_gate(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.kv_cache.kv_cache_model_provider."
        "is_kv_cache_affinity_enabled",
        lambda: enabled,
    )


async def _drain_product_actions() -> None:
    while kv_cache_product_hooks._PRODUCT_GUARD_TASKS:
        await asyncio.gather(
            *tuple(kv_cache_product_hooks._PRODUCT_GUARD_TASKS),
            return_exceptions=True,
        )


def test_resolve_switch_context_keeps_product_facts_when_affinity_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_gate(monkeypatch, False)
    monkeypatch.setattr(
        kv_cache_product_hooks.session_metadata,
        "get_session_metadata",
        lambda session_id: {"mode": "team" if session_id == "old" else "code.normal"},
    )

    context = kv_cache_product_hooks.resolve_session_switch_context(
        target_session_id="new-session",
        previous_session_id="old",
        params={"mode": "code.normal"},
    )

    assert context == kv_cache_product_hooks.SessionSwitchContext(
        target_is_team=False,
        previous_is_team=True,
        resolved_mode="code.normal",
        affinity_enabled=False,
    )


def test_resolve_switch_context_contains_affinity_gate_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.kv_cache.kv_cache_model_provider."
        "is_kv_cache_affinity_enabled",
        lambda: (_ for _ in ()).throw(RuntimeError("broken config")),
    )
    monkeypatch.setattr(
        kv_cache_product_hooks.session_metadata,
        "get_session_metadata",
        lambda _session_id: {},
    )

    context = kv_cache_product_hooks.resolve_session_switch_context(
        target_session_id="session-a",
        previous_session_id="",
        params={"mode": "code.normal"},
    )

    assert context.affinity_enabled is False
    assert context.resolved_mode == "code.normal"


@pytest.mark.asyncio
async def test_navigation_records_visibility_without_prefetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        kv_cache_product_hooks.session_history,
        "history_exists",
        lambda _session_id: True,
    )
    context = kv_cache_product_hooks.SessionSwitchContext(
        target_is_team=False,
        previous_is_team=False,
        resolved_mode="code.normal",
        affinity_enabled=True,
    )

    await kv_cache_product_hooks.dispatch_session_switch_signals(
        context=context,
        channel_id="web",
        target_session_id="session-b",
        previous_session_id="session-a",
    )

    assert kv_cache_product_hooks._PRODUCT_GUARD_TASKS == set()


@pytest.mark.asyncio
async def test_prepare_dispatches_only_session_prepare(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_gate(monkeypatch, True)
    monkeypatch.setattr(
        kv_cache_product_hooks.session_metadata,
        "get_session_metadata",
        lambda _session_id: {"mode": "code.normal"},
    )
    monkeypatch.setattr(
        kv_cache_product_hooks.session_history,
        "history_exists",
        lambda _session_id: True,
    )
    calls: list[str] = []

    class _Session:
        async def prepare_kvc(self) -> bool:
            calls.append("prepare")
            return True

        async def suspend_kvc(self) -> bool:
            calls.append("suspend")
            return True

    monkeypatch.setattr(
        "openjiuwen.core.session.agent.create_agent_session",
        lambda **_kwargs: _Session(),
    )

    result = kv_cache_product_hooks.record_session_prepare(
        session_id="session-a",
        intent_id="intent-a",
        channel_id="web",
        params={"mode": "code.normal"},
    )
    await _drain_product_actions()

    assert result == "scheduled"
    assert calls == ["prepare"]


@pytest.mark.asyncio
async def test_background_completion_dispatches_session_suspend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_gate(monkeypatch, True)
    monkeypatch.setattr(
        kv_cache_product_hooks.session_metadata,
        "get_session_metadata",
        lambda _session_id: {"mode": "code.normal"},
    )
    monkeypatch.setattr(
        kv_cache_product_hooks.session_history,
        "history_exists",
        lambda _session_id: False,
    )
    calls: list[str] = []

    class _Session:
        async def prepare_kvc(self) -> bool:
            calls.append("prepare")
            return True

        async def suspend_kvc(self) -> bool:
            calls.append("suspend")
            return True

    monkeypatch.setattr(
        "openjiuwen.core.session.agent.create_agent_session",
        lambda **_kwargs: _Session(),
    )

    await kv_cache_product_hooks.record_chat_started(
        session_id="session-a",
        params={"mode": "code.normal"},
        channel_id="web",
    )
    kv_cache_product_hooks.record_chat_finished(
        session_id="session-a",
        succeeded=True,
    )
    await _drain_product_actions()

    assert calls == ["suspend"]


@pytest.mark.asyncio
async def test_team_action_uses_team_session_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class _Session:
        async def suspend_kvc(self) -> bool:
            calls.append("team-suspend")
            return True

        async def prepare_kvc(self) -> bool:
            calls.append("team-prepare")
            return True

    monkeypatch.setattr(
        "openjiuwen.core.session.agent_team.create_agent_team_session",
        lambda **_kwargs: _Session(),
    )
    action = SimpleNamespace(
        action="offload",
        session_id="team-a",
        is_team=True,
    )

    kv_cache_product_hooks._dispatch_guard_action(action)
    await _drain_product_actions()

    assert calls == ["team-suspend"]


def test_successful_delete_forgets_product_facts() -> None:
    from jiuwenswarm.server.runtime.session.kv_cache.kv_cache_task_guard import (
        get_session_kv_cache_task_guard,
    )

    guard = get_session_kv_cache_task_guard()
    guard.set_foreground(
        session_id="session-a",
        view_id="view-a",
        visible=True,
        channel_id="web",
        is_team=False,
    )

    kv_cache_product_hooks.forget_deleted_session("session-a")

    assert guard.snapshot("session-a") is None
