# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import pytest

from agent_gateway.common.protocol_ids import (
    InvalidProtocolId,
    SESSION_ID_MAX_LEN,
    is_valid_session_id,
    validate_session_id,
)
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.request_policy import request_policy_error


def test_session_id_boundary_matches_persistent_session_contract() -> None:
    assert validate_session_id("a" * SESSION_ID_MAX_LEN) == "a" * SESSION_ID_MAX_LEN
    assert is_valid_session_id("sess-valid_1") is True
    assert is_valid_session_id("a" * (SESSION_ID_MAX_LEN + 1)) is False


@pytest.mark.parametrize("value", (None, 1, "", " sess", "sess ", ".hidden", "hidden-"))
def test_session_id_rejects_invalid_values_without_echoing_input(value) -> None:
    with pytest.raises(InvalidProtocolId) as caught:
        validate_session_id(value)
    assert str(caught.value) in {
        "session_id must be a string",
        "invalid session_id",
    }


def test_workflow_run_id_is_bounded() -> None:
    valid = "run-" + "x" * 252
    request = AgentRequest(request_id="run", req_method="swarmflow.pause", params={"run_id": valid})
    assert request_policy_error(request) is None
    request.params["run_id"] += "x"
    response = request_policy_error(request)
    assert response.payload["code"] == "BAD_REQUEST"
    assert "exceeds maximum length" in response.payload["error"]


@pytest.mark.parametrize("value", (None, 1, "", " run-1", "run-1 ", "run\n1"))
def test_workflow_run_id_rejects_invalid_values(value) -> None:
    request = AgentRequest(request_id="run", req_method="swarmflow.pause", params={"run_id": value})
    assert request_policy_error(request).payload["code"] == "BAD_REQUEST"
