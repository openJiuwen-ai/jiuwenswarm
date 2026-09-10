"""Group rewriting uses conversation context without local profile files."""

from types import SimpleNamespace

from jiuwenswarm.gateway.im_pipeline.im_inbound import IMConversationProcessor, IMHistoryMessage


def test_group_rewrite_keeps_history_and_pending_context_without_profile():
    processor = object.__new__(IMConversationProcessor)
    adapter = SimpleNamespace(
        load_recent_messages=lambda thread_id, limit: [
            IMHistoryMessage("sender", "Alice", "Earlier project decision", 1000),
        ],
        resolve_user_display_name=lambda sender: "Alice",
    )

    prompt = processor._build_prompt(
        thread_id="group", sender_user_id="sender", text="Current project question",
        timestamp_ms=2000, principal_name="Owner", adapter=adapter,
        pending_context="Pending clarification",
    )

    assert "Earlier project decision" in prompt
    assert "Current project question" in prompt
    assert "Pending clarification" in prompt
    assert "用户画像" not in prompt
