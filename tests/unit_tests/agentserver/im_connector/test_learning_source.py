from __future__ import annotations

import json

import pytest

from jiuwenswarm.server.im.im_connector.cli_runtime import MockCliRunner
from jiuwenswarm.server.im.im_connector.connectors.dingtalk import DingTalkCli, DingTalkConnector
from jiuwenswarm.server.im.im_connector.connectors.welink import MockWelinkCli, WeLinkConnector
from jiuwenswarm.server.im.im_connector.learning_source import ConnectorLearningSource
from jiuwenswarm.server.im.im_connector.registry import ConnectorRegistry
from openjiuwen.harness.personal_context.im import ImLearningCursor, ImLearningTarget


@pytest.mark.asyncio
async def test_connector_learning_source_maps_fetch_and_advances_cursor():
    mock = MockWelinkCli()
    mock.enqueue_history(json.dumps({
        "respData": {
            "chatInfo": [
                {"sender": "alice", "content": "newer", "serverSendTime": 2000, "msgId": "m2"},
                {"sender": "bob", "content": "older", "serverSendTime": 1000, "msgId": "m1"},
            ]
        }
    }))
    registry = ConnectorRegistry([WeLinkConnector(mock, self_account="alice")])
    source = ConnectorLearningSource(registry)
    batch = await source.fetch_messages(
        ImLearningTarget(channel_id="welink", kind="group", external_id="g1"),
        ImLearningCursor(count=10),
    )
    assert len(batch.messages) == 2
    assert batch.messages[0].msg_id == "m2"
    assert batch.next_cursor is not None
    assert batch.next_cursor.message_id == "m1"
    assert batch.next_cursor.query_direction == 0
    assert not any(args[0] == "im" and "send-to-group" in args for args, _ in mock.calls)


@pytest.mark.asyncio
async def test_connector_learning_source_uses_platform_page_token():
    runner = MockCliRunner()
    cli = DingTalkCli({"cli_path": "dws"}, runner=runner)
    connector = DingTalkConnector(cli, self_account="DGUself")
    runner.enqueue_history(json.dumps({
        "messages": [
            {
                "createTime": "2026-09-20 15:32:02",
                "messageId": "msg-new",
                "senderId": "DGUself",
                "text": "newer",
            },
            {
                "createTime": "2026-09-20 15:22:43",
                "messageId": "msg-old",
                "senderId": "DGUother",
                "text": "older",
            },
        ],
        "hasMore": True,
        "nextPageToken": "dws-chat-v1.token",
    }))
    runner.enqueue_history(json.dumps({"messages": [], "hasMore": False}))
    source = ConnectorLearningSource(ConnectorRegistry([connector]))
    target = ImLearningTarget(channel_id="dingtalk", kind="group", external_id="cid_g")
    first = await source.fetch_messages(target, ImLearningCursor(count=10))
    assert first.next_cursor is not None
    assert first.next_cursor.extra["page_token"] == "dws-chat-v1.token"
    assert first.next_cursor.message_id is None
    await source.fetch_messages(target, first.next_cursor)
    args = runner.calls[1][0]
    assert args[args.index("--page-token") + 1] == "dws-chat-v1.token"
    assert "msg-old" not in args


@pytest.mark.asyncio
async def test_connector_learning_source_requires_registered_channel():
    source = ConnectorLearningSource(ConnectorRegistry())
    with pytest.raises(KeyError, match="welink"):
        await source.fetch_messages(
            ImLearningTarget(channel_id="welink", kind="group", external_id="g1"),
        )
