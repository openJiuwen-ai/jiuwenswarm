# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Tests for agentserver utility functions."""

from jiuwenswarm.server.utils.utils import get_chat_id
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod


def test_get_chat_id_from_top_level_field() -> None:
    """测试从顶层字段获取 Chat ID"""
    request = AgentRequest(
        request_id="test1",
        channel_id="feishu",
        session_id="session1",
        chat_id="oc_test123",
        req_method=ReqMethod.CHAT_SEND,
    )
    assert get_chat_id(request) == "oc_test123"


def test_get_chat_id_from_metadata_wecom() -> None:
    """测试从 metadata 获取企微 Chat ID"""
    request = AgentRequest(
        request_id="test2",
        channel_id="wecom",
        session_id="session2",
        req_method=ReqMethod.CHAT_SEND,
        metadata={"wecom_chat_id": "wecom_chat_456"},
    )
    assert get_chat_id(request) == "wecom_chat_456"


def test_get_chat_id_from_metadata_feishu() -> None:
    """测试从 metadata 获取飞书 Chat ID"""
    request = AgentRequest(
        request_id="test3",
        channel_id="feishu",
        session_id="session3",
        req_method=ReqMethod.CHAT_SEND,
        metadata={"feishu_chat_id": "feishu_chat_789"},
    )
    assert get_chat_id(request) == "feishu_chat_789"


def test_get_chat_id_from_metadata_dingtalk() -> None:
    """测试从 metadata 获取钉钉 Chat ID"""
    request = AgentRequest(
        request_id="test4",
        channel_id="dingtalk",
        session_id="session4",
        req_method=ReqMethod.CHAT_SEND,
        metadata={"dingtalk_chat_id": "dingtalk_chat_012"},
    )
    assert get_chat_id(request) == "dingtalk_chat_012"


def test_get_chat_id_from_metadata_xiaoyi() -> None:
    """测试从 metadata 获取小艺 Chat ID"""
    request = AgentRequest(
        request_id="test5",
        channel_id="xiaoyi",
        session_id="session5",
        req_method=ReqMethod.CHAT_SEND,
        metadata={"xiaoyi_session_id": "xiaoyi_session_345"},
    )
    assert get_chat_id(request) == "xiaoyi_session_345"


def test_get_chat_id_prioritizes_top_level_over_metadata() -> None:
    """测试顶层字段优先于 metadata"""
    request = AgentRequest(
        request_id="test6",
        channel_id="dingtalk",
        session_id="session6",
        chat_id="top_level_789",
        req_method=ReqMethod.CHAT_SEND,
        metadata={"dingtalk_chat_id": "should_not_use"},
    )
    assert get_chat_id(request) == "top_level_789"


def test_get_chat_id_returns_none_when_not_available() -> None:
    """测试没有可用的 Chat ID 时返回 None"""
    request = AgentRequest(
        request_id="test7",
        channel_id="unknown",
        session_id="session7",
        req_method=ReqMethod.CHAT_SEND,
    )
    assert get_chat_id(request) is None


def test_get_chat_id_returns_none_when_metadata_is_empty() -> None:
    """测试 metadata 为空时返回 None"""
    request = AgentRequest(
        request_id="test8",
        channel_id="unknown",
        session_id="session8",
        req_method=ReqMethod.CHAT_SEND,
        metadata={},
    )
    assert get_chat_id(request) is None
