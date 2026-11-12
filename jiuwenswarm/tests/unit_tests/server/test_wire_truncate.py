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


def test_ask_user_question_is_restorable_history_event() -> None:
    # 问题澄清对话框刷新后需从历史恢复，event_type 必须在后端白名单里放行。
    assert "chat.ask_user_question" in _HISTORY_RESTORABLE_ASSISTANT_EVENT_TYPES


def test_ask_user_answer_is_restorable_history_event() -> None:
    # 问题澄清答案刷新后需从历史恢复回显，event_type 必须在后端白名单里放行。
    assert "chat.ask_user_answer" in _HISTORY_RESTORABLE_ASSISTANT_EVENT_TYPES


def test_chat_error_is_restorable_history_event() -> None:
    # chat.error 落盘后（interface.py:4040）刷新/恢复会话时需从历史恢复展示，
    # event_type 必须在后端白名单里放行，否则 read_history_cursor_page 在
    # _is_restorable_history_record 处丢弃，前端收不到。
    assert "chat.error" in _HISTORY_RESTORABLE_ASSISTANT_EVENT_TYPES
