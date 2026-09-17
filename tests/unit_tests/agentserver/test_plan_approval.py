# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for plan approval utilities.

These NLP-based intent classifiers are kept as a fallback for text-based
approval in channels without TUI interrupt support.
"""

# pylint: disable=protected-access

from jiuwenswarm.agents.harness.code.prompt.plan_approval import (
    classify_plan_user_intent,
    is_direct_plan_implement_request,
)
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer


def test_direct_plan_implement_requires_strong_signal() -> None:
    assert is_direct_plan_implement_request("好，那按计划实现吧") is True
    assert is_direct_plan_implement_request("按计划实现") is True
    assert is_direct_plan_implement_request("好") is False
    assert is_direct_plan_implement_request("可以") is False


def test_classify_implement_intent_as_approve() -> None:
    assert classify_plan_user_intent("按计划实现") == "approve"
    assert classify_plan_user_intent("开始实现吧") == "approve"
    assert classify_plan_user_intent("implement the plan") == "approve"


def test_classify_revision_intent() -> None:
    assert classify_plan_user_intent("多添加几个边界测试用例") == "revise"
    assert classify_plan_user_intent("第二步改成异步") == "revise"
    assert classify_plan_user_intent("不行，先别做") == "revise"


def test_classify_mixed_revision_overrides_short_approval() -> None:
    assert classify_plan_user_intent("可以，但是要把第二步改成异步") == "revise"


def test_skills_list_does_not_sync_code_mode() -> None:
    request = AgentRequest(
        request_id="req_skills",
        channel_id="tui",
        session_id="sess_skills",
        req_method=ReqMethod.SKILLS_LIST,
        params={"mode": "code.normal"},
    )
    assert AgentWebSocketServer._should_sync_code_mode_state(request) is False
