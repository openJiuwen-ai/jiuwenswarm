"""Failure safety for the retained dev-stable run-span compatibility API."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

from jiuwenswarm.agents.harness import agent_observability


def test_close_legacy_handle_ends_span_when_binding_removal_fails(monkeypatch) -> None:
    span = Mock()
    span.get_span_context.return_value.trace_id = 123
    binding_store = Mock()
    binding_store.remove.side_effect = RuntimeError("already removed")
    handle = agent_observability._LegacyRunSpanHandle(span, object(), binding_store)

    agent_observability.close_agent_run_span(handle, session_id="session-a")
    agent_observability.close_agent_run_span(handle, session_id="session-a")

    span.end.assert_called_once_with()
    binding_store.remove.assert_called_once()


def test_close_legacy_api_accepts_raw_span() -> None:
    span = Mock()
    span.get_span_context.return_value.trace_id = 123

    agent_observability.close_agent_run_span(span, session_id="session-a")

    span.end.assert_called_once_with()


def test_open_legacy_span_rolls_back_failed_context_setup(monkeypatch) -> None:
    from openjiuwen.agent_teams.observability import span_context as team_context
    from openjiuwen.extensions.observability import setup, span_context as root_context

    span = Mock()
    tracer = Mock()
    tracer.start_span.return_value = span
    runtime = SimpleNamespace(
        is_unified_active=lambda: False,
        trace_bindings=Mock(),
    )
    monkeypatch.setattr(agent_observability, "_get_unified_runtime", lambda: runtime)
    monkeypatch.setattr(agent_observability, "_agent_observability_active", True)
    monkeypatch.setattr(setup, "is_initialized", lambda: True)
    monkeypatch.setattr(setup, "get_tracer", lambda _name: tracer)
    monkeypatch.setattr(team_context, "set_team_span", Mock())
    monkeypatch.setattr(team_context, "get_team_span", lambda: span)
    clear_team_span = Mock()
    monkeypatch.setattr(team_context, "clear_team_span", clear_team_span)
    monkeypatch.setattr(root_context, "set_root_span", Mock(side_effect=RuntimeError("context failed")))
    clear_root_span = Mock()
    monkeypatch.setattr(root_context, "clear_root_span", clear_root_span)

    assert agent_observability.open_agent_run_span(session_id="session-a") is None
    span.end.assert_called_once_with()
    clear_team_span.assert_called_once_with()
    clear_root_span.assert_called_once_with(session_id="session-a", expected_span=span)
