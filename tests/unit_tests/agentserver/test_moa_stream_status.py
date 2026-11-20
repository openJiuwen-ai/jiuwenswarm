# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""MoA progress frames expose lifecycle metadata without model output."""

from types import SimpleNamespace

import pytest

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
from jiuwenswarm.server.utils.stream_utils import parse_stream_chunk


@pytest.mark.parametrize("parser", [parse_stream_chunk, JiuWenSwarmDeepAdapter.parse_stream_chunk])
@pytest.mark.parametrize("phase", ["reference", "verification"])
@pytest.mark.parametrize("status", ["started", "completed", "failed", "cancelled"])
def test_moa_status_projects_only_lifecycle_metadata(parser, phase: str, status: str) -> None:
    payload = {
        "operation_id": "operation-a",
        "phase": phase,
        "status": status,
        "content": "private model output",
        "prompt": "private verification prompt",
        "score": 0.7,
    }

    parsed = parser(SimpleNamespace(type="chat.moa_status", payload=payload))

    assert parsed == {
        "event_type": "chat.moa_status",
        "operation_id": "operation-a",
        "phase": phase,
        "status": status,
    }


@pytest.mark.parametrize("parser", [parse_stream_chunk, JiuWenSwarmDeepAdapter.parse_stream_chunk])
@pytest.mark.parametrize("wrapped", [False, True])
def test_moa_status_dict_frames_preserve_routing_without_exposing_details(parser, wrapped: bool) -> None:
    payload = {
        "operation_id": "operation-a",
        "phase": "reference",
        "status": "started",
        "session_id": "session-a",
        "execution_id": "execution-a",
        "content": "private output",
    }
    chunk = {"type": "chat.moa_status", "payload": payload} if wrapped else {
        "event_type": "chat.moa_status", **payload,
    }

    assert parser(chunk) == {
        "event_type": "chat.moa_status",
        **{key: value for key, value in payload.items() if key != "content"},
    }


@pytest.mark.parametrize("parser", [parse_stream_chunk, JiuWenSwarmDeepAdapter.parse_stream_chunk])
@pytest.mark.parametrize("invalid", [
    {"operation_id": ""},
    {"operation_id": None},
    {"phase": "aggregation"},
    {"phase": []},
    {"status": "running"},
    {"status": {}},
])
def test_moa_status_ignores_invalid_lifecycle_frames(parser, invalid: dict) -> None:
    payload = {"operation_id": "operation-a", "phase": "verification", "status": "started", **invalid}

    assert parser(SimpleNamespace(type="chat.moa_status", payload=payload)) is None
