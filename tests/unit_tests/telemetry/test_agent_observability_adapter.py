"""Unified and legacy agent observability adapter contracts."""

from __future__ import annotations

import asyncio
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from jiuwenswarm.extensions.identity_provider import IdentityInfo, IdentityStore
from jiuwenswarm.telemetry.attributes import (
    APP_ID,
    DOMAIN_ID,
    GEN_AI_CONVERSATION_ID,
    JIUWENCLAW_APP_ID,
    JIUWENCLAW_CHANNEL_ID,
    JIUWENCLAW_DOMAIN_ID,
    JIUWENCLAW_REQUEST_ID,
    JIUWENCLAW_SESSION_ID,
    JIUWENCLAW_USER_ID,
    USER_ID,
)
from jiuwenswarm.telemetry.request_context import TraceBindingRegistry


class _Span:
    def __init__(self, name: str, trace_id: int, span_id: int) -> None:
        self.name = name
        self.context = SimpleNamespace(trace_id=trace_id, span_id=span_id)
        self.attributes: dict[str, object] = {}
        self.end_calls = 0

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value

    def get_span_context(self) -> object:
        return self.context

    def is_recording(self) -> bool:
        return self.end_calls == 0

    def end(self) -> None:
        self.end_calls += 1


class _Tracer:
    def __init__(self) -> None:
        self.spans: list[_Span] = []

    def start_span(self, *, name: str, kind: object) -> _Span:
        del kind
        sequence = len(self.spans) + 1
        span = _Span(name, trace_id=sequence, span_id=sequence + 100)
        self.spans.append(span)
        return span


class _TracerProvider:
    def __init__(self, tracer: _Tracer) -> None:
        self.tracer = tracer

    def get_tracer(self, name: str) -> _Tracer:
        assert name == "jiuwenswarm.agent"
        return self.tracer


def _unified_runtime() -> SimpleNamespace:
    tracer = _Tracer()
    return SimpleNamespace(
        is_unified_active=lambda: True,
        tracer_provider=_TracerProvider(tracer),
        span_registry=Mock(),
        trace_bindings=TraceBindingRegistry(max_bindings=32, ttl_seconds=60),
        tracer=tracer,
    )


def test_unified_root_spans_are_rich_and_stale_close_is_isolated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openjiuwen.agent_teams.observability import span_context
    from jiuwenswarm.agents.harness import agent_observability as adapter

    runtime = _unified_runtime()
    monkeypatch.setattr(adapter, "_get_unified_runtime", lambda: runtime)
    cascade = Mock()
    flush = Mock()
    monkeypatch.setattr(span_context, "cascade_close_children", cascade)
    monkeypatch.setattr(span_context, "flush_child_spans", flush)
    identity_token = IdentityStore.set_identity(
        IdentityInfo(user_id="u1", domain_id="d1", app_id="a1")
    )
    try:
        old = adapter.open_agent_run_span(
            session_id="s1",
            request_id="r1",
            channel_id="c1",
            mode="code.plan",
        )
        current = adapter.open_agent_run_span(
            session_id="s1",
            request_id="r3",
            channel_id="c3",
            mode="code.plan",
        )

        assert old is not None and current is not None
        assert runtime.trace_bindings.resolve_session("s1") is current.binding
        expected = {
            GEN_AI_CONVERSATION_ID: "s1",
            JIUWENCLAW_SESSION_ID: "s1",
            JIUWENCLAW_REQUEST_ID: "r1",
            JIUWENCLAW_CHANNEL_ID: "c1",
            "jiuwenswarm.mode": "code.plan",
            USER_ID: "u1",
            JIUWENCLAW_USER_ID: "u1",
            DOMAIN_ID: "d1",
            JIUWENCLAW_DOMAIN_ID: "d1",
            APP_ID: "a1",
            JIUWENCLAW_APP_ID: "a1",
        }
        assert old.root_span.attributes.items() >= expected.items()
        runtime.span_registry.bind_trace_attributes.assert_any_call(
            old.root_span.context.trace_id,
            old.root_span.attributes,
        )

        adapter.close_agent_run_span(old, session_id="s1")
        adapter.close_agent_run_span(old, session_id="s1")
        assert runtime.trace_bindings.resolve_session("s1") is current.binding
        assert old.root_span.end_calls == 1
        assert current.root_span.end_calls == 0
        assert span_context.get_team_span() is current.root_span

        adapter.close_agent_run_span(current, session_id="s1")
        adapter.close_agent_run_span(current, session_id="s1")
        assert runtime.trace_bindings.resolve_session("s1") is None
        assert current.root_span.end_calls == 1
        assert span_context.get_team_span() is None
        assert flush.call_args_list == [
            call(trace_id=old.root_span.context.trace_id),
            call(trace_id=current.root_span.context.trace_id),
        ]
        cascade.assert_called_once_with()
    finally:
        IdentityStore.clear(identity_token)
        span_context.clear_team_span()


def test_registry_fallback_resolves_each_supervisor_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openjiuwen.extensions.observability.callback_handler as callback_handler
    from openjiuwen.agent_teams.context import reset_session_id, set_session_id
    from openjiuwen.agent_teams.observability import span_context
    from jiuwenswarm.agents.harness import agent_observability as adapter

    runtime = _unified_runtime()
    monkeypatch.setattr(adapter, "_get_unified_runtime", lambda: runtime)
    monkeypatch.setattr(span_context, "cascade_close_children", Mock())
    monkeypatch.setattr(span_context, "flush_child_spans", Mock())

    async def run_concurrently() -> tuple[tuple[object, _Span], tuple[object, _Span]]:
        gate = asyncio.Event()

        async def resolve(session_id: str, request_id: str) -> tuple[object, _Span]:
            token = set_session_id(session_id)
            try:
                handle = adapter.open_agent_run_span(
                    session_id=session_id,
                    request_id=request_id,
                    channel_id=f"channel-{session_id}",
                    mode="agent.fast",
                )
                assert handle is not None
                span_context.clear_team_span()
                await gate.wait()
                resolved = callback_handler.get_root_span()
                assert span_context.get_team_span() is resolved
                adapter.close_agent_run_span(handle, session_id=session_id)
                return handle, resolved
            finally:
                span_context.clear_team_span()
                reset_session_id(token)

        first = asyncio.create_task(resolve("s1", "r1"))
        second = asyncio.create_task(resolve("s2", "r2"))
        await asyncio.sleep(0)
        gate.set()
        return await first, await second

    (handle1, resolved1), (handle2, resolved2) = asyncio.run(run_concurrently())

    assert resolved1 is handle1.root_span
    assert resolved2 is handle2.root_span
    assert handle1.root_span.end_calls == 1
    assert handle2.root_span.end_calls == 1
    assert runtime.trace_bindings.resolve_session("s1") is None
    assert runtime.trace_bindings.resolve_session("s2") is None


def test_trace_binding_rail_restores_request_root_in_supervisor_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openjiuwen.agent_teams.observability import span_context
    from jiuwenswarm.agents.harness import agent_observability as adapter

    runtime = _unified_runtime()
    monkeypatch.setattr(adapter, "_get_unified_runtime", lambda: runtime)
    monkeypatch.setattr(span_context, "cascade_close_children", Mock())
    monkeypatch.setattr(span_context, "flush_child_spans", Mock())
    handle = adapter.open_agent_run_span(
        session_id="supervisor-session",
        request_id="supervisor-request",
        channel_id="web",
        mode="code.normal",
    )
    assert handle is not None
    span_context.clear_team_span()
    context = SimpleNamespace(
        session=SimpleNamespace(get_session_id=lambda: "supervisor-session")
    )

    async def bind_in_supervisor() -> object:
        await adapter.AgentTraceBindingRail().before_task_iteration(context)
        return span_context.get_team_span()

    assert asyncio.run(bind_in_supervisor()) is handle.root_span
    adapter.close_agent_run_span(handle, session_id="supervisor-session")


def test_unified_sync_never_reinitializes_or_shuts_down_agentcore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openjiuwen.agent_teams.observability as core_observability
    from jiuwenswarm.agents.harness import agent_observability as agent_adapter
    from jiuwenswarm.agents.harness.team import team_manager

    runtime = _unified_runtime()
    monkeypatch.setattr(agent_adapter, "_get_unified_runtime", lambda: runtime)
    monkeypatch.setattr(team_manager, "_get_unified_runtime", lambda: runtime)
    monkeypatch.setattr(
        agent_adapter,
        "get_config",
        Mock(side_effect=AssertionError("legacy agent config must not be read")),
    )
    monkeypatch.setattr(
        team_manager,
        "get_config",
        Mock(side_effect=AssertionError("legacy team config must not be read")),
    )
    init = Mock()
    shutdown = Mock()
    monkeypatch.setattr(core_observability, "init_observability", init)
    monkeypatch.setattr(core_observability, "shutdown_observability", shutdown)
    monkeypatch.setattr(agent_adapter, "_agent_observability_active", False)
    monkeypatch.setattr(agent_adapter, "_agent_owns_provider", False)
    monkeypatch.setattr(agent_adapter, "_runtime_managed_agent_observability", False)
    monkeypatch.setattr(agent_adapter, "_force_ever_enabled", False)
    monkeypatch.setattr(team_manager, "_observability_active", False)
    monkeypatch.setattr(team_manager, "_runtime_managed_observability", False)

    agent_adapter.sync_agent_observability(force=True)
    assert agent_adapter._force_ever_enabled is False
    team_manager.sync_team_observability()
    agent_adapter.shutdown_agent_observability()
    team_manager.shutdown_team_observability()

    init.assert_not_called()
    shutdown.assert_not_called()
    assert agent_adapter._agent_observability_active is False
    assert team_manager._observability_active is False


def test_unified_force_does_not_make_later_legacy_provider_sticky(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openjiuwen.agent_teams.observability as core_observability
    from jiuwenswarm.agents.harness import agent_observability as adapter
    from jiuwenswarm.agents.harness import observability_runtime

    observability_runtime.reset_observability_demands()
    runtime_active = True
    runtime = _unified_runtime()
    runtime.is_unified_active = lambda: runtime_active
    config_enabled = True
    monkeypatch.setattr(adapter, "_get_unified_runtime", lambda: runtime)
    monkeypatch.setattr(
        adapter,
        "get_config",
        lambda: {"agent_observability": {"enabled": config_enabled}},
    )
    state = {"initialized": False}

    def _is_initialized() -> bool:
        return state["initialized"]

    def _init(_config, **_kwargs) -> None:
        state["initialized"] = True

    monkeypatch.setattr(core_observability, "is_initialized", _is_initialized)
    monkeypatch.setattr(core_observability, "ObservabilityConfig", SimpleNamespace)
    init = Mock(side_effect=_init)
    shutdown = Mock(side_effect=lambda: state.__setitem__("initialized", False))
    monkeypatch.setattr(core_observability, "init_observability", init)
    monkeypatch.setattr(core_observability, "shutdown_observability", shutdown)
    monkeypatch.setattr(adapter, "_agent_observability_active", False)
    monkeypatch.setattr(adapter, "_agent_owns_provider", False)
    monkeypatch.setattr(adapter, "_runtime_managed_agent_observability", False)
    monkeypatch.setattr(adapter, "_force_ever_enabled", False)

    adapter.sync_agent_observability(force=True)
    assert adapter._force_ever_enabled is False

    runtime_active = False
    adapter.sync_agent_observability()
    assert init.call_count == 1
    assert adapter._agent_owns_provider is True

    config_enabled = False
    adapter.sync_agent_observability()
    shutdown.assert_called_once_with()
    assert adapter._agent_observability_active is False


def test_legacy_root_span_still_uses_agentcore_tracer_and_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openjiuwen.agent_teams.observability as core_observability
    from openjiuwen.agent_teams.observability import span_context
    from openjiuwen.extensions.observability import setup as ext_setup
    from jiuwenswarm.agents.harness import agent_observability as adapter

    tracer = _Tracer()
    runtime = SimpleNamespace(
        is_unified_active=lambda: False,
        trace_bindings=TraceBindingRegistry(max_bindings=8, ttl_seconds=60),
    )
    monkeypatch.setattr(adapter, "_get_unified_runtime", lambda: runtime)
    monkeypatch.setattr(core_observability, "is_initialized", lambda: True)
    monkeypatch.setattr(ext_setup, "is_initialized", lambda: True)
    monkeypatch.setattr(ext_setup, "get_tracer", lambda _name: tracer)
    monkeypatch.setattr(span_context, "cascade_close_children", Mock())
    monkeypatch.setattr(span_context, "flush_child_spans", Mock())
    monkeypatch.setattr(adapter, "_agent_observability_active", True)
    monkeypatch.setattr(adapter, "_agent_owns_provider", True)
    monkeypatch.setattr(adapter, "_runtime_managed_agent_observability", False)

    handle = adapter.open_agent_run_span(
        session_id="legacy-s",
        request_id="legacy-r",
        channel_id="legacy-c",
        mode="agent.fast",
    )

    assert handle is not None
    assert handle.unified is False
    assert handle.root_span.name == "agent.agent.fast.legacy-s"
    assert runtime.trace_bindings.resolve("legacy-s", "legacy-r") is handle.binding
    adapter.close_agent_run_span(handle, session_id="legacy-s")
    assert handle.root_span.end_calls == 1
    assert runtime.trace_bindings.resolve("legacy-s", "legacy-r") is None
    span_context.clear_team_span()


def test_real_adapter_call_sites_match_core_run_span_signature() -> None:
    source_path = (
        Path(__file__).parents[3]
        / "jiuwenswarm/server/runtime/agent_adapter/interface_deep.py"
    )
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "open_agent_run_span"
    ]

    assert len(calls) == 2
    for node_call in calls:
        keyword_names = {keyword.arg for keyword in node_call.keywords}
        assert keyword_names >= {
            "session_id",
            "request_id",
            "mode",
        }
        assert "channel_id" not in keyword_names
        mode_keyword = next(
            keyword for keyword in node_call.keywords if keyword.arg == "mode"
        )
        assert isinstance(mode_keyword.value, ast.Call)
        assert isinstance(mode_keyword.value.func, ast.Name)
        assert mode_keyword.value.func.id == "_resolve_observability_mode"
