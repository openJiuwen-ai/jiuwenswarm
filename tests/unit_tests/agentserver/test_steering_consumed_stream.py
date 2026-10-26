# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Ordered steering display boundaries are root/leader-only protocol metadata."""

from enum import Enum
from types import SimpleNamespace

import pytest

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


class Role(Enum):
    LEADER = "leader"
    TEAMMATE = "teammate"


@pytest.mark.parametrize("role", [None, "leader", Role.LEADER])
def test_root_and_leader_consumed_boundary_preserves_fifo_ids(role):
    chunk = SimpleNamespace(type="steering_consumed", role=role,
                            payload={"input_ids": ["first", "second", "first"]})
    assert JiuWenSwarmDeepAdapter._parse_stream_chunk(chunk) == {
        "event_type": "chat.steering_consumed", "input_ids": ["first", "second"],
    }


@pytest.mark.parametrize("role", ["teammate", Role.TEAMMATE, "worker"])
def test_member_consumption_cannot_divide_root_conversation(role):
    chunk = SimpleNamespace(type="steering_consumed", role=role, payload={"input_ids": ["private"]})
    assert JiuWenSwarmDeepAdapter._parse_stream_chunk(chunk) is None


@pytest.mark.parametrize("payload", [None, {}, {"input_ids": []}, {"input_ids": "one"},
                                    {"input_ids": [1]}, {"input_ids": [" "]}])
def test_invalid_consumption_metadata_never_becomes_visible_text(payload):
    chunk = SimpleNamespace(type="steering_consumed", payload=payload)
    assert JiuWenSwarmDeepAdapter._parse_stream_chunk(chunk) is None
