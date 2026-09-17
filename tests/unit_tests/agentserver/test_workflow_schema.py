# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Tests for workflow-related enum additions in message schema."""

from jiuwenswarm.common.schema.message import EventType, ReqMethod


def test_req_method_command_workflows():
    """Verify COMMAND_WORKFLOWS enum value exists with correct string."""
    assert ReqMethod.COMMAND_WORKFLOWS.value == "command.workflows"


def test_event_type_workflow_updated():
    """Verify WORKFLOW_UPDATED enum value exists with correct string."""
    assert EventType.WORKFLOW_UPDATED.value == "workflow.updated"


def test_req_method_chat_swarmflow_reply():
    """Verify CHAT_SWARMFLOW_REPLY enum value exists with correct string."""
    assert ReqMethod.CHAT_SWARMFLOW_REPLY.value == "chat.swarmflow_reply"


def test_swarmflow_reply_params_typeddict_keys():
    """SwarmflowReplyParams declares the expected keys (total=False => all optional)."""
    from jiuwenswarm.common.schema.swarmflow_reply import SwarmflowReplyParams

    hints = SwarmflowReplyParams.__annotations__
    for key in ("session_id", "team_name", "run_id", "correlation_id", "answer"):
        assert key in hints, f"missing key {key}"