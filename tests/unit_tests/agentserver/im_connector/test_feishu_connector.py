from __future__ import annotations

import json

import pytest

from jiuwenswarm.server.im.im_connector.cli_runtime import MockCliRunner
from jiuwenswarm.server.im.im_connector.connectors.feishu import FeishuCli, FeishuConnector
from jiuwenswarm.server.im.im_connector.types import ChannelTarget, FetchOptions, ImMessage, SendOptions


@pytest.fixture
def feishu():
    runner = MockCliRunner()
    cli = FeishuCli({"cli_path": "lark-cli"}, runner=runner)
    connector = FeishuConnector(cli, self_account="ou_self")
    return connector, runner


@pytest.mark.asyncio
async def test_feishu_fetch_maps_lark_messages(feishu):
    connector, runner = feishu
    runner.enqueue_history(json.dumps({
        "messages": [
            {
                "message_id": "om_1",
                "msg_type": "text",
                "create_time": "1000",
                "sender": {"id": "ou_self", "name": "Me"},
                "content": "{\"text\":\"hi\"}",
            },
            {
                "message_id": "om_2",
                "create_time": 2000,
                "sender": {"id": "ou_bob", "name": "Bob"},
                "content": {"text": "there"},
            },
        ]
    }))
    page = await connector.fetch_messages(
        ChannelTarget(kind="group", external_id="oc_g"),
        FetchOptions(count=10),
    )
    assert [item.msg_id for item in page.messages] == ["om_1", "om_2"]
    assert page.messages[0].is_self is True
    assert page.messages[1].content_text == "there"
    assert runner.calls[0][0][:2] == ["im", "+chat-messages-list"]
    assert "--as" in runner.calls[0][0] and "user" in runner.calls[0][0]


@pytest.mark.asyncio
async def test_feishu_fetch_maps_lark_cli_envelope_and_system_messages(feishu):
    connector, runner = feishu
    runner.enqueue_history(json.dumps({
        "ok": True,
        "data": {
            "has_more": False,
            "messages": [
                {
                    "message_id": "om_sys",
                    "msg_type": "system",
                    "create_time": "2026-09-21 09:01",
                    "content": "Welcome to {group_type}",
                    "sender": {"id": "", "sender_type": ""},
                }
            ],
        },
    }))
    page = await connector.fetch_messages(
        ChannelTarget(kind="group", external_id="oc_g"),
        FetchOptions(count=10),
    )
    assert len(page.messages) == 1
    assert page.messages[0].msg_id == "om_sys"
    assert page.messages[0].content_text == "Welcome to {group_type}"
    assert page.messages[0].sent_at > 0
    assert page.has_more is False


@pytest.mark.asyncio
async def test_feishu_send_uses_user_identity(feishu):
    connector, runner = feishu
    runner.enqueue_send(ok=True)
    result = await connector.send_message(ChannelTarget(kind="user", external_id="ou_bob"), "hello")
    assert result.ok is True
    args = runner.calls[0][0]
    assert args[:2] == ["im", "+messages-send"]
    assert "--user-id" in args
    assert "来自 ou_self 的数字分身：hello" in args


@pytest.mark.asyncio
async def test_feishu_send_mentions_open_id_in_markdown(feishu):
    connector, runner = feishu
    connector._self_display_name = "用户023770"
    runner.enqueue_send(ok=True)
    source = ImMessage(
        channel_id="feishu",
        msg_id="src",
        conversation_external_id="oc_g",
        sender_account="ou_xk",
        sender_name="许康",
        content_text="hi",
        sent_at=1,
    )
    result = await connector.send_message(
        ChannelTarget(kind="group", external_id="oc_g"),
        "欢迎",
        SendOptions(mention_sender=True, extra={"source_message": source}),
    )
    assert result.ok is True
    args = runner.calls[0][0]
    assert "--markdown" in args
    text = args[args.index("--markdown") + 1]
    assert '<at user_id="ou_xk"></at>' in text
    assert "来自 用户023770 的数字分身：欢迎" in text


@pytest.mark.asyncio
async def test_feishu_resolve_identity_from_auth_json(feishu):
    connector, runner = feishu
    runner.enqueue_auth_status(json.dumps({
        "identities": {"user": {"open_id": "ou_self", "name": "Alice", "available": True}}
    }))
    identity = await connector.resolve_identity()
    assert identity is not None
    assert identity.account == "ou_self"


@pytest.mark.asyncio
async def test_feishu_resolve_identity_from_lark_cli_camel_case(feishu):
    connector, runner = feishu
    runner.enqueue_auth_status(json.dumps({
        "identities": {
            "user": {
                "openId": "ou_live",
                "userName": "用户023770",
                "available": True,
                "verified": True,
            }
        }
    }))
    identity = await connector.resolve_identity()
    assert identity is not None
    assert identity.account == "ou_live"
    assert identity.display_name == "用户023770"


@pytest.mark.asyncio
async def test_feishu_resolve_identity_prefers_localized_name(feishu):
    connector, runner = feishu
    runner.enqueue_auth_status(json.dumps({
        "identities": {
            "user": {
                "openId": "ou_live",
                "userName": "用户023770",
                "available": True,
            }
        }
    }))
    runner.enqueue_self_person(json.dumps({
        "ok": True,
        "data": {
            "users": [{
                "open_id": "ou_live",
                "localized_name": "小蚊子哈哈哈",
            }]
        },
    }))
    identity = await connector.resolve_identity()
    assert identity is not None
    assert identity.display_name == "小蚊子哈哈哈"
    args = runner.calls[1][0]
    assert args[:2] == ["contact", "+search-user"]
    assert "--user-ids" in args and "me" in args
    assert "--lang" in args and "zh_cn" in args


@pytest.mark.asyncio
async def test_feishu_discover_conversations_reads_data_chats(feishu):
    connector, runner = feishu
    runner.enqueue_recent_conversations(json.dumps({
        "ok": True,
        "identity": "user",
        "data": {
            "chats": [
                {
                    "chat_id": "oc_group",
                    "chat_mode": "group",
                    "name": "数字分身",
                },
                {
                    "chat_id": "oc_dm",
                    "chat_mode": "p2p",
                    "name": "许康",
                    "p2p_target_id": "ou_xk",
                },
            ],
            "has_more": False,
        },
    }))
    rows = await connector.discover_conversations(query_count=20)
    assert [row.kind for row in rows] == ["group", "user"]
    assert rows[0].external_id == "oc_group"
    assert rows[1].external_id == "ou_xk"
    assert runner.calls[0][0][:2] == ["im", "+chat-list"]


@pytest.mark.asyncio
async def test_feishu_search_persons_uses_search_user_shortcut(feishu):
    connector, runner = feishu
    runner.enqueue_search_persons(json.dumps({
        "data": {"users": [{"open_id": "ou_xk", "localized_name": "许康"}]}
    }))
    people = await connector.search_persons("许康")
    assert people[0].account == "ou_xk"
    assert people[0].display_name == "许康"
    args = runner.calls[0][0]
    assert args[:2] == ["contact", "+search-user"]
    assert "--query" in args
