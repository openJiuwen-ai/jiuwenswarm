# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""集成测试：验证从 Channel 到 AgentServer 的 Chat ID 传递链路."""

import time

from jiuwenswarm.server.utils.utils import get_chat_id
from jiuwenswarm.common.e2a.agent_compat import e2a_to_agent_request
from jiuwenswarm.common.e2a.gateway_normalize import message_to_e2a_or_fallback
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import Message, ReqMethod


def test_feishu_channel_to_agentserver_chat_id():
    """测试飞书 Channel → Gateway → AgentServer 的 Chat ID 传递"""
    # 1. 模拟飞书 Channel 创建 Message
    msg = Message(
        id="feishu_msg_123",
        type="req",
        channel_id="feishu",
        session_id="oc_aaaaaaa",
        params={"content": "你好"},
        timestamp=time.time(),
        ok=True,
        provider="feishu",
        chat_id="oc_aaaaaaa",  # 飞书 Chat ID
        user_id="ou_xxxxxxx",
        bot_id="cli_xxxxxxx",
        req_method=ReqMethod.CHAT_SEND,
        is_stream=True,
        metadata={
            "message_id": "feishu_msg_123",
            "chat_type": "p2p",
            "msg_type": "text",
            "open_id": "ou_xxxxxxx",
            "feishu_open_id": "ou_xxxxxxx",
            "feishu_chat_id": "oc_aaaaaaa",
        },
    )

    # 2. Gateway: Message → E2AEnvelope
    env = message_to_e2a_or_fallback(msg)
    assert env.request_id == "feishu_msg_123"
    assert env.channel == "feishu"
    assert env.chat_id == "oc_aaaaaaa"

    # 3. AgentServer: E2AEnvelope → AgentRequest
    request = e2a_to_agent_request(env)
    assert request.request_id == "feishu_msg_123"
    assert request.channel_id == "feishu"
    assert request.chat_id == "oc_aaaaaaa"

    # 4. 验证 get_chat_id() 能正确获取
    chat_id = get_chat_id(request)
    assert chat_id == "oc_aaaaaaa"


def test_wecom_channel_to_agentserver_chat_id():
    """测试企微 Channel → Gateway → AgentServer 的 Chat ID 传递"""
    # 1. 模拟企微 Channel 创建 Message
    msg = Message(
        id="wecom_msg_456",
        type="req",
        channel_id="wecom",
        session_id="wecom_chatid",
        params={"content": "你好", "query": "你好"},
        timestamp=time.time(),
        ok=True,
        req_method=ReqMethod.CHAT_SEND,
        is_stream=True,
        chat_id="wecom_chatid",  # 企微 Chat ID
        metadata={
            "wecom_chat_id": "wecom_chatid",
            "wecom_req_id": "wecom_msg_456",
        },
    )

    # 2. Gateway: Message → E2AEnvelope
    env = message_to_e2a_or_fallback(msg)
    assert env.request_id == "wecom_msg_456"
    assert env.channel == "wecom"
    assert env.chat_id == "wecom_chatid"

    # 3. AgentServer: E2AEnvelope → AgentRequest
    request = e2a_to_agent_request(env)
    assert request.request_id == "wecom_msg_456"
    assert request.channel_id == "wecom"
    assert request.chat_id == "wecom_chatid"

    # 4. 验证 get_chat_id() 能正确获取
    chat_id = get_chat_id(request)
    assert chat_id == "wecom_chatid"


def test_dingtalk_channel_to_agentserver_chat_id():
    """测试钉钉 Channel → Gateway → AgentServer 的 Chat ID 传递"""
    # 1. 模拟钉钉 Channel 创建 Message
    msg = Message(
        id="dingtalk_msg_789",
        type="req",
        channel_id="dingtalk",
        session_id="dingtalk_sender_id",
        params={"content": "你好", "query": "你好"},
        timestamp=time.time(),
        ok=True,
        req_method=ReqMethod.CHAT_SEND,
        chat_id="dingtalk_conversation_id",  # 钉钉 Chat ID
        metadata={
            "conversation_id": "dingtalk_conversation_id",
            "conversation_type": "1",
            "dingtalk_chat_id": "dingtalk_conversation_id",
            "dingtalk_sender_id": "dingtalk_sender_id",
            "sender_name": "张三",
        },
    )

    # 2. Gateway: Message → E2AEnvelope
    env = message_to_e2a_or_fallback(msg)
    assert env.request_id == "dingtalk_msg_789"
    assert env.channel == "dingtalk"
    assert env.chat_id == "dingtalk_conversation_id"

    # 3. AgentServer: E2AEnvelope → AgentRequest
    request = e2a_to_agent_request(env)
    assert request.request_id == "dingtalk_msg_789"
    assert request.channel_id == "dingtalk"
    assert request.chat_id == "dingtalk_conversation_id"

    # 4. 验证 get_chat_id() 能正确获取
    chat_id = get_chat_id(request)
    assert chat_id == "dingtalk_conversation_id"


def test_xiaoyi_channel_to_agentserver_chat_id():
    """测试小艺 Channel → Gateway → AgentServer 的 Chat ID 传递"""
    # 1. 模拟小艺 Channel 创建 Message
    msg = Message(
        id="xiaoyi_msg_012",
        type="req",
        channel_id="xiaoyi",
        session_id="xiaoyi_session_id",
        params={"query": "帮我查天气"},
        timestamp=time.time(),
        is_stream=True,
        ok=True,
        req_method=ReqMethod.CHAT_SEND,
        chat_id="xiaoyi_session_id",  # 小艺 Chat ID
        metadata={
            "method": "message/stream",
            "xiaoyi_session_id": "xiaoyi_session_id",
            "xiaoyi_task_id": "xiaoyi_task_id",
        },
    )

    # 2. Gateway: Message → E2AEnvelope
    env = message_to_e2a_or_fallback(msg)
    assert env.request_id == "xiaoyi_msg_012"
    assert env.channel == "xiaoyi"
    assert env.chat_id == "xiaoyi_session_id"

    # 3. AgentServer: E2AEnvelope → AgentRequest
    request = e2a_to_agent_request(env)
    assert request.request_id == "xiaoyi_msg_012"
    assert request.channel_id == "xiaoyi"
    assert request.chat_id == "xiaoyi_session_id"

    # 4. 验证 get_chat_id() 能正确获取
    chat_id = get_chat_id(request)
    assert chat_id == "xiaoyi_session_id"


def test_all_channels_chat_id_unified_interface():
    """测试所有平台通过统一接口 get_chat_id() 获取 Chat ID"""
    # 构建四个平台的 AgentRequest
    requests = {
        "feishu": AgentRequest(
            request_id="feishu",
            channel_id="feishu",
            session_id="s1",
            chat_id="oc_feishu",
            req_method=ReqMethod.CHAT_SEND,
            metadata={"feishu_chat_id": "oc_feishu"},
        ),
        "wecom": AgentRequest(
            request_id="wecom",
            channel_id="wecom",
            session_id="s2",
            chat_id="wecom_chat",
            req_method=ReqMethod.CHAT_SEND,
            metadata={"wecom_chat_id": "wecom_chat"},
        ),
        "dingtalk": AgentRequest(
            request_id="dingtalk",
            channel_id="dingtalk",
            session_id="s3",
            chat_id="dingtalk_chat",
            req_method=ReqMethod.CHAT_SEND,
            metadata={"dingtalk_chat_id": "dingtalk_chat"},
        ),
        "xiaoyi": AgentRequest(
            request_id="xiaoyi",
            channel_id="xiaoyi",
            session_id="s4",
            chat_id="xiaoyi_session",
            req_method=ReqMethod.CHAT_SEND,
            metadata={"xiaoyi_session_id": "xiaoyi_session"},
        ),
    }

    # 验证所有平台都能通过统一接口获取 Chat ID
    expected = {
        "feishu": "oc_feishu",
        "wecom": "wecom_chat",
        "dingtalk": "dingtalk_chat",
        "xiaoyi": "xiaoyi_session",
    }

    for platform, request in requests.items():
        chat_id = get_chat_id(request)
        assert chat_id == expected.get(platform), f"{platform} 的 Chat ID 不正确"


def test_metadata_fallback_when_chat_id_missing():
    """测试顶层 chat_id 缺失时，能正确从 metadata 获取"""
    # 测试场景：只有 metadata，没有顶层 chat_id
    request = AgentRequest(
        request_id="fallback_test",
        channel_id="wecom",
        session_id="s1",
        chat_id=None,  # 顶层字段为空
        req_method=ReqMethod.CHAT_SEND,
        metadata={"wecom_chat_id": "fallback_chatid"},
    )

    # 应该从 metadata 获取
    chat_id = get_chat_id(request)
    assert chat_id == "fallback_chatid"


def test_top_level_priority_over_metadata():
    """测试顶层 chat_id 优先于 metadata"""
    request = AgentRequest(
        request_id="priority_test",
        channel_id="feishu",
        session_id="s1",
        chat_id="top_level_id",  # 顶层字段
        req_method=ReqMethod.CHAT_SEND,
        metadata={"feishu_chat_id": "metadata_id"},  # metadata
    )

    # 应该优先使用顶层字段
    chat_id = get_chat_id(request)
    assert chat_id == "top_level_id"
