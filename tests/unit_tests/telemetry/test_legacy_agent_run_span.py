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
