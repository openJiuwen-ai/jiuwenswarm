# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bug #4851: invalid permission resume payloads must not silently fall back to first_check.

When a non-parseable user_input (typically a chat message string accidentally routed
into the response slot) reaches ``JiuwenSwarmPermissionInterruptRail.resolve_interrupt``,
the rail must:

  * log an ``invalid_permission_resume`` audit event with input type / related IDs /
    reason — but **never** the raw chat content (privacy + PII concern),
  * reject the call, **not** fall back to ``first_check`` (avoid fail-open allow on a
    stale tool that may have a persisted allow rule),
  * not re-issue the interrupt (the original bug was that the rail silently re-issued
    the card, losing the chat semantics and skipping the audit trail).

The Host input boundary (``_discard_superseded_permission_before_fresh_input``) is the
*root cause* fix; this test only locks down the rail's defense layer so that if the
boundary fix regresses, the rail still produces a safe, auditable failure.
"""

from __future__ import annotations

import logging

import pytest
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.interrupt.interrupt_base import (
    ApproveResult,
    InterruptResult,
    RejectResult,
)
from openjiuwen.harness.rails.security.tool_security_rail import PermissionInterruptRail
from openjiuwen.harness.security import PermissionLevel

from jiuwenswarm.agents.harness.common.rails.permissions.permission_interrupt_rail import (
    JiuwenSwarmPermissionInterruptRail,
)

# jiuwenswarm.common.utils:3030 sets up its module logger under the
# ``jiuwenswarm`` root logger, and jiuwenswarm.common.utils:2871 disables
# propagate on that root. Pytest's ``caplog`` fixture hooks the Python root
# logger, so without re-enabling propagation the audit warning emitted from
# ``_reject_unparseable_resume`` reaches stderr but never enters caplog.


@pytest.fixture(autouse=True)
def _enable_audit_propagation():
    """Temporarily re-enable propagation on the ``jiuwenswarm`` root logger so
    pytest's ``caplog`` fixture can capture audit warnings emitted from
    ``jiuwenswarm.common.utils``."""
    root = logging.getLogger("jiuwenswarm")
    original = root.propagate
    root.propagate = True
    try:
        yield
    finally:
        root.propagate = original


def _bash_call(call_id: str, command: str):
    """Return a minimal ToolCall-shaped object the rail can introspect."""
    return type(
        "ToolCall",
        (),
        {
            "id": call_id,
            "name": "bash",
            "arguments": {"command": command},
        },
    )()


def _ctx():
    """Return a minimal AgentCallbackContext for resolve_interrupt."""
    session = type("Session", (), {"get_state": lambda self, key: None, "update_state": lambda self, *a, **k: None})()
    return AgentCallbackContext(
        agent=None,
        inputs=None,
        extra={},
        session=session,
    )


@pytest.mark.asyncio
async def test_str_user_input_is_audited_and_rejected_without_first_check(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Chat text in the response slot must produce an audit log and a RejectResult.

    Even when a session-allow rule covers the tool, the rail must NOT fall back to
    first_check (which would let the persisted rule approve the stale tool call).
    """
    rail = JiuwenSwarmPermissionInterruptRail(
        config={
            "enabled": True,
            "tools": {"bash": "ask"},
            "defaults": {"*": "ask"},
            "rules": [],
            # Persisted allow that would normally let "git status" through.
            "approval_overrides": [{"match": "git status", "decision": "allow"}],
        }
    )

    with caplog.at_level(logging.WARNING, logger="jiuwenswarm.common.utils"):
        decision = await rail.resolve_interrupt(
            ctx=_ctx(),
            tool_call=_bash_call("t1", "git status"),
            user_input="echo 你好",  # chat text — not a parseable auth payload
        )

    assert isinstance(decision, RejectResult), (
        "chat text in the response slot must be rejected, not silently executed via "
        f"first_check or re-issued with an interrupt (got {type(decision).__name__})"
    )
    assert "invalid_permission_resume" in caplog.text
    assert "str" in caplog.text or "user_input_type" in caplog.text
    assert "bash" in caplog.text  # tool name surfaced, NOT raw chat content
    # Privacy: raw chat text must never appear in the audit log.
    assert "echo 你好" not in caplog.text


@pytest.mark.asyncio
async def test_none_user_input_falls_through_to_normal_first_check() -> None:
    """user_input=None is the canonical first entry; no audit, normal flow."""
    rail = JiuwenSwarmPermissionInterruptRail(
        config={
            "enabled": True,
            "tools": {},  # no explicit per-tool rule
            "defaults": {"*": "allow"},  # everything defaults to allow
            "rules": [],
            "approval_overrides": [],
        }
    )

    decision = await rail.resolve_interrupt(
        ctx=_ctx(),
        tool_call=_bash_call("t2", "git status"),
        user_input=None,
    )

    assert isinstance(decision, ApproveResult), (
        "user_input=None must run the regular first_check path (got "
        f"{type(decision).__name__})"
    )


@pytest.mark.asyncio
async def test_malformed_dict_payload_is_audited_and_rejected(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A malformed dict that fails ConfirmPayload validation must also be rejected."""
    rail = JiuwenSwarmPermissionInterruptRail(
        config={
            "enabled": True,
            "tools": {"bash": "ask"},
            "defaults": {"*": "allow"},
            "rules": [],
            "approval_overrides": [{"match": "git status", "decision": "allow"}],
        }
    )

    with caplog.at_level(logging.WARNING, logger="jiuwenswarm.common.utils"):
        decision = await rail.resolve_interrupt(
            ctx=_ctx(),
            tool_call=_bash_call("t3", "git status"),
            user_input={"approved": "maybe"},  # not a bool — fails ConfirmPayload
        )

    assert isinstance(decision, RejectResult), (
        "malformed dict payload must also be rejected, not silently executed via "
        f"first_check (got {type(decision).__name__})"
    )
    assert "invalid_permission_resume" in caplog.text


@pytest.mark.asyncio
async def test_well_formed_payload_still_works() -> None:
    """A properly structured InteractiveInput-shaped payload must keep its approve flow."""
    rail = JiuwenSwarmPermissionInterruptRail(
        config={
            "enabled": True,
            "tools": {"bash": "ask"},
            "defaults": {"*": "ask"},
            "rules": [],
            "approval_overrides": [],
        }
    )

    decision = await rail.resolve_interrupt(
        ctx=_ctx(),
        tool_call=_bash_call("t4", "git status"),
        user_input={
            "approved": True,
            "auto_confirm": False,
            "persist_allow": False,
            "feedback": "",
        },
    )

    assert isinstance(decision, ApproveResult), (
        "a properly formatted auth payload must continue to work (got "
        f"{type(decision).__name__})"
    )