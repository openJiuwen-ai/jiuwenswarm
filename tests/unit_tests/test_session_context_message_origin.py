# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for provenance on messages restored from product Session history."""

from openjiuwen.core.foundation.llm.schema.message import (
    OPENJIUWEN_MESSAGE_ORIGIN_EXTERNAL_USER,
    OPENJIUWEN_MESSAGE_ORIGIN_METADATA,
    OPENJIUWEN_MESSAGE_SOURCE_KIND_METADATA,
    UserMessage,
)

from jiuwenswarm.agents.harness.common.session_ops_service import (
    _build_context_messages_from_history,
)


def test_restored_product_session_users_remain_external_inputs() -> None:
    messages, skipped = _build_context_messages_from_history([{
        "role": "user",
        "content": "original browser input",
        "channel_id": "web",
    }])

    assert skipped == 0
    assert len(messages) == 1
    assert isinstance(messages[0], UserMessage)
    assert messages[0].metadata[OPENJIUWEN_MESSAGE_ORIGIN_METADATA] == (
        OPENJIUWEN_MESSAGE_ORIGIN_EXTERNAL_USER
    )
    assert messages[0].metadata[OPENJIUWEN_MESSAGE_SOURCE_KIND_METADATA] == "web"
