from __future__ import annotations

import json

import pytest

from jiuwenswarm.server.im.im_connector.cli_runtime import MockCliRunner
from jiuwenswarm.server.im.im_connector.connectors.dingtalk import DingTalkCli, DingTalkConnector
from jiuwenswarm.server.im.im_connector.types import ChannelTarget, FetchOptions, ImMessage, SendOptions

_SELF_OPEN_ID = "DGUselfOpenId"
_SELF_USER_ID = "431651395023738965"


@pytest.fixture
def dingtalk():
    runner = MockCliRunner()
    cli = DingTalkCli({"cli_path": "dws"}, runner=runner)
    connector = DingTalkConnector(cli, self_account=_SELF_OPEN_ID)
    return connector, runner


@pytest.mark.asyncio
async def test_dingtalk_fetch_maps_dws_messages(dingtalk):
    connector, runner = dingtalk
    runner.enqueue_history(json.dumps({
        "contractVersion": "im.message-list.v1",
        "messages": [
            {
                "conversationId": "cidxxx",
                "createTime": "2026-09-20 15:32:02",
                "messageId": "msg1==",
                "sender": "小蚊子",
                "senderId": _SELF_OPEN_ID,
                "text": "connector probe",
            },
            {
                "conversationId": "cidxxx",
                "createTime": "2026-09-20 15:22:43",
                "messageId": "msg2==",
                "sender": "同事",
                "senderId": "DGUother",
                "text": "[黑眼圈]",
            },
        ],
        "hasMore": True,
        "nextPageToken": "dws-chat-v1.token",
    }))
    page = await connector.fetch_messages(
        ChannelTarget(kind="group", external_id="cid_g"),
        FetchOptions(count=10),
    )
    msgs = page.messages
    assert [item.msg_id for item in msgs] == ["msg1==", "msg2=="]
    assert msgs[0].is_self is True
    assert msgs[0].content_text == "connector probe"
    assert msgs[0].sender_name == "小蚊子"
    assert msgs[1].is_self is False
    assert page.next_page_token == "dws-chat-v1.token"
    assert page.has_more is True
    assert "--limit" in runner.calls[0][0]
    assert runner.calls[0][0][:2] == ["chat", "+chat-messages"]
    assert "--page-token" not in runner.calls[0][0]


@pytest.mark.asyncio
async def test_dingtalk_fetch_passes_page_token_not_message_id(dingtalk):
    connector, runner = dingtalk
    runner.enqueue_history(json.dumps({"messages": [], "hasMore": False}))
    await connector.fetch_messages(
        ChannelTarget(kind="group", external_id="cid_g"),
        FetchOptions(count=10, page_token="dws-chat-v1.token", message_id="msg1=="),
    )
    args = runner.calls[0][0]
    assert "--page-token" in args
    assert args[args.index("--page-token") + 1] == "dws-chat-v1.token"
    assert "msg1==" not in args


@pytest.mark.asyncio
async def test_dingtalk_send_uses_user_identity(dingtalk):
    connector, runner = dingtalk
    connector._self_display_name = "小蚊子"
    runner.enqueue_send(ok=True)
    result = await connector.send_message(ChannelTarget(kind="group", external_id="cid_g"), "hello")
    assert result.ok is True
    args = runner.calls[0][0]
    assert args[:2] == ["chat", "+messages-send"]
    assert "--group" in args
    assert "--text" in args
    assert "--yes" in args
    assert "--content" not in args
    assert "--as" in args and "user" in args
    text = args[args.index("--text") + 1]
    assert text == "来自 小蚊子 的数字分身：hello"
    assert _SELF_OPEN_ID not in text


@pytest.mark.asyncio
async def test_dingtalk_resolve_identity_uses_open_dingtalk_id(dingtalk):
    connector, runner = dingtalk
    runner.enqueue_auth_status(json.dumps({
        "success": True,
        "result": [{"orgEmployeeModel": {"userId": _SELF_USER_ID, "orgUserName": "小蚊子"}}],
    }))
    runner.enqueue_search_persons(json.dumps({
        "success": True,
        "result": [{
            "name": "小蚊子",
            "openDingTalkId": _SELF_OPEN_ID,
            "userId": _SELF_USER_ID,
        }],
    }))
    identity = await connector.resolve_identity()
    assert identity is not None
    assert identity.account == _SELF_OPEN_ID
    assert identity.display_name == "小蚊子"
    assert identity.extra["user_id"] == _SELF_USER_ID
    assert identity.extra["open_dingtalk_id"] == _SELF_OPEN_ID
    assert _SELF_OPEN_ID in identity.extra["open_ids"]
    assert _SELF_USER_ID in identity.extra["open_ids"]
    assert runner.calls[1][0][runner.calls[1][0].index("--query") + 1] == _SELF_USER_ID


@pytest.mark.asyncio
async def test_dingtalk_resolve_identity_ignores_same_display_name(dingtalk):
    connector, runner = dingtalk
    runner.enqueue_auth_status(json.dumps({
        "success": True,
        "result": [{"orgEmployeeModel": {"userId": _SELF_USER_ID, "orgUserName": "小蚊子"}}],
    }))
    runner.enqueue_search_persons(json.dumps({
        "success": True,
        "result": [{
            "name": "小蚊子",
            "openDingTalkId": "DGUsomeoneElse",
            "userId": "999",
        }],
    }))
    identity = await connector.resolve_identity()
    assert identity is not None
    assert identity.account == _SELF_USER_ID
    assert identity.extra["open_dingtalk_id"] is None
    assert "DGUsomeoneElse" not in identity.extra["open_ids"]


@pytest.mark.asyncio
async def test_dingtalk_is_self_matches_any_self_id(dingtalk):
    connector, runner = dingtalk
    connector._self_accounts = {_SELF_OPEN_ID, _SELF_USER_ID}
    runner.enqueue_history(json.dumps({
        "messages": [
            {
                "createTime": "2026-09-20 15:32:02",
                "messageId": "msg1==",
                "senderId": _SELF_USER_ID,
                "text": "org id",
            },
            {
                "createTime": "2026-09-20 15:32:03",
                "messageId": "msg2==",
                "senderId": _SELF_OPEN_ID,
                "text": "open id",
            },
        ]
    }))
    page = await connector.fetch_messages(
        ChannelTarget(kind="group", external_id="cid_g"),
        FetchOptions(count=10),
    )
    assert [item.is_self for item in page.messages] == [True, True]


@pytest.mark.asyncio
async def test_dingtalk_send_mentions_open_id_placeholder(dingtalk):
    connector, runner = dingtalk
    runner.enqueue_send(ok=True)
    source = ImMessage(
        channel_id="dingtalk",
        msg_id="src",
        conversation_external_id="cid_g",
        sender_account="DGUother",
        sender_name="同事",
        content_text="hi",
        sent_at=1,
    )
    result = await connector.send_message(
        ChannelTarget(kind="group", external_id="cid_g"),
        "hello",
        SendOptions(mention_sender=True, extra={"source_message": source}),
    )
    assert result.ok is True
    args = runner.calls[0][0]
    text = args[args.index("--text") + 1]
    assert "<@DGUother>" in text
    assert "@同事" not in text
    assert "--at-open-dingtalk-ids" in args
    assert args[args.index("--at-open-dingtalk-ids") + 1] == "DGUother"


@pytest.mark.asyncio
async def test_dingtalk_discover_conversations_uses_chat_list(dingtalk):
    connector, runner = dingtalk
    runner.enqueue_recent_conversations(json.dumps({
        "chats": [
            {
                "chatMode": "group",
                "conversationType": "group",
                "name": "数字分身讨论",
                "openConversationId": "cidloxMLX2bSKUQPGOoXsE+Bg==",
            },
            {
                "chatMode": "p2p",
                "conversationType": "direct",
                "name": "许康",
                "openConversationId": "cid_dm_1",
                "openDingTalkId": "DGUother",
            },
        ],
        "hasMore": False,
    }))
    rows = await connector.discover_conversations(query_count=20)
    args = runner.calls[0][0]
    assert args[:2] == ["chat", "+chat-list"]
    assert "--page-size" in args
    assert args[args.index("--page-size") + 1] == "20"
    assert "--types" in args
    assert "group,p2p" in args
    assert "--page-all" in args
    assert "--page-limit" in args
    assert "--query" not in args
    assert "+chat-search" not in args
    assert rows[0].kind == "group"
    assert rows[0].external_id == "cidloxMLX2bSKUQPGOoXsE+Bg=="
    assert rows[0].title == "数字分身讨论"
    assert rows[1].kind == "user"
    assert rows[1].external_id == "DGUother"
    assert rows[1].title == "许康"
