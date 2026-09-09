from jiuwenswarm.server.wire_truncate import (
    _HISTORY_COLLAPSE_KEEP_KEYS,
    _HISTORY_RESTORABLE_ASSISTANT_EVENT_TYPES,
)


def test_subagent_roster_updates_are_restorable_history_events() -> None:
    assert "chat.subtask_update" in _HISTORY_RESTORABLE_ASSISTANT_EVENT_TYPES


def test_agent_identity_survives_oversized_history_collapse() -> None:
    assert "agent_template_name" in _HISTORY_COLLAPSE_KEEP_KEYS


def test_context_usage_is_restorable_history_event() -> None:
    assert "context.usage" in _HISTORY_RESTORABLE_ASSISTANT_EVENT_TYPES
