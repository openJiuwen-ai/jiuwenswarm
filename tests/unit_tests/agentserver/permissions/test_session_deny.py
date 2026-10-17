"""Tests for private same-session denial matching."""

from jiuwenswarm.agents.harness.common.rails.permissions.session_deny import (
    SessionDenyStore,
    evaluate_session_deny,
)


def test_same_session_and_exact_args_remain_denied() -> None:
    store = SessionDenyStore()
    args = {"cmd": "rm -rf build"}
    store.record_denial(
        session_id="s1",
        tool_name="bash",
        tool_args=args,
        reason="user_rejected",
    )

    decision = evaluate_session_deny(
        store,
        session_id="s1",
        tool_name="bash",
        tool_args=args,
    )

    assert decision is not None
    assert decision.reason == "session_user_denied"


def test_session_or_args_change_does_not_match() -> None:
    store = SessionDenyStore()
    store.record_denial(
        session_id="s1",
        tool_name="bash",
        tool_args={"cmd": "echo one"},
        reason="user_rejected",
    )

    assert evaluate_session_deny(
        store,
        session_id="s2",
        tool_name="bash",
        tool_args={"cmd": "echo one"},
    ) is None
    assert evaluate_session_deny(
        store,
        session_id="s1",
        tool_name="bash",
        tool_args={"cmd": "echo two"},
    ) is None
