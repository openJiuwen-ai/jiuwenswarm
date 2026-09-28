# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Represent Host-delivered Agent messages at tool authority in model history."""

from hashlib import sha256
from uuid import uuid4

from openjiuwen.core.foundation.llm.schema.message import (
    OPENJIUWEN_MESSAGE_ORIGIN_HARNESS_INTERNAL,
    OPENJIUWEN_MESSAGE_ORIGIN_METADATA,
    OPENJIUWEN_MESSAGE_SOURCE_KIND_METADATA,
    AssistantMessage,
    ToolMessage,
)
from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall


def cross_session_model_messages(content: str, cross_session: dict) -> list:
    """Use a Host-generated call/result pair: the SDK requires paired tool results.

    The pair is an input record, not an executed model tool call. Source content
    belongs only in the tool result, never in assistant instructions.
    """
    identity = str(cross_session.get("message_id") or uuid4().hex)
    call_id = "sm_" + sha256(identity.encode()).hexdigest()[:24]
    name = "session_message_received"
    metadata = {
        OPENJIUWEN_MESSAGE_ORIGIN_METADATA: OPENJIUWEN_MESSAGE_ORIGIN_HARNESS_INTERNAL,
        OPENJIUWEN_MESSAGE_SOURCE_KIND_METADATA: "agent_session",
        "host_generated": True,
        "cross_session": dict(cross_session),
    }
    return [
        AssistantMessage(
            content="", metadata=dict(metadata),
            tool_calls=[ToolCall(id=call_id, type="function", name=name, arguments="{}")],
        ),
        ToolMessage(
            content=str(content), name=name, tool_call_id=call_id,
            metadata=dict(metadata),
        ),
    ]
