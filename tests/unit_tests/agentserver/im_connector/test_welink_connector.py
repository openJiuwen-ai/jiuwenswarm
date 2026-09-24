from __future__ import annotations

import json

import pytest

from jiuwenswarm.server.im.im_connector.connectors.welink import MockWelinkCli, WeLinkConnector
from jiuwenswarm.server.im.im_connector.connectors.welink.cli import CliResult
from jiuwenswarm.server.im.im_connector.connectors.welink.parser import (
    parse_auth_status_uid,
    parse_history_messages,
    parse_person_search_results,
    parse_recent_conversations,
    to_im_message,
)
from jiuwenswarm.server.im.im_connector.reply import format_outgoing_reply_text
from jiuwenswarm.server.im.im_connector.types import ChannelTarget, FetchOptions


def test_parse_history_messages_extracts_chatinfo_messages():
    raw = {
        "respData": {
            "chatInfo": [
                {"sender": "alice", "content": "hello", "serverSendTime": 1000, "msgId": "m1"},
                {"sender": "bob", "content": "world", "serverSendTime": 2000, "msgId": "m2"},
            ]
        }
    }
    msgs = parse_history_messages(json.dumps(raw))
    assert len(msgs) == 2
    assert msgs[0]["msgId"] == "m1"


def test_parse_history_messages_skips_invalid_messages():
    raw = {
        "respData": {
            "chatInfo": [
                {"sender": "alice", "content": "hello", "serverSendTime": 1000},
                {"sender": "", "content": "skip", "serverSendTime": 2000},
                {"sender": "bob", "content": "skip", "serverSendTime": -1},
                {"sender": "bob", "content": "valid", "serverSendTime": 3000, "msgId": "m3"},
            ]
        }
    }
    msgs = parse_history_messages(json.dumps(raw))
    assert len(msgs) == 2
    assert msgs[1]["msgId"] == "m3"


def test_parse_person_search_results_extracts_w3account():
    raw = {
        "search_cli_person": {
            "data": [
                {"w3account": "u1", "welinkId": "w1", "name": "Alice"},
                {"w3account": "u2", "welinkId": "w2", "userName": "Bob"},
                {"w3account": "u3", "welinkId": ""},
                {"w3account": "", "welinkId": "w4"},
            ]
        }
    }
    persons = parse_person_search_results(json.dumps(raw))
    assert len(persons) == 2
    assert persons[1]["name"] == "Bob"


def test_parse_auth_status_uid_extracts_uid_from_output():
    assert parse_auth_status_uid("WeLink CLI\nUID: abc123def\nLogin: active") == "abc123def"
    assert parse_auth_status_uid("no uid here") == ""


def test_parse_recent_conversations_splits_group_and_user():
    stdout = json.dumps({
        "conversation_info": [
            {"group_id": "g1", "group_name": "Release", "target_account": ""},
            {"group_id": "0", "group_name": "", "target_account": "bob", "native_name": "Bob"},
        ]
    })
    rows = parse_recent_conversations(stdout)
    assert rows == [
        {"kind": "group", "external_id": "g1", "title": "Release"},
        {"kind": "user", "external_id": "bob", "title": "Bob"},
    ]


def test_to_im_message_maps_fields_and_resolves_is_self():
    raw = {"sender": "alice", "content": "hello", "serverSendTime": 1000, "msgId": "m1"}
    msg = to_im_message(
        raw, channel_id="welink", conversation_external_id="group-a", is_self_account="alice"
    )
    assert msg.is_self is True
    other = to_im_message(
        raw, channel_id="welink", conversation_external_id="group-a", is_self_account="bob"
    )
    assert other.is_self is False


@pytest.fixture
def plugin_with_mock():
    mock = MockWelinkCli()
    plugin = WeLinkConnector(mock, self_account="alice")
    return plugin, mock


@pytest.mark.asyncio
async def test_welink_connector_fetch_messages_maps_to_im_message(plugin_with_mock):
    plugin, mock = plugin_with_mock
    mock.enqueue_history(json.dumps({
        "respData": {
            "chatInfo": [
                {"sender": "alice", "content": "my msg", "serverSendTime": 1000, "msgId": "m1"},
                {"sender": "bob", "content": "their msg", "serverSendTime": 2000, "msgId": "m2"},
            ]
        }
    }))
    target = ChannelTarget(kind="group", external_id="group-a", title="Release")
    page = await plugin.fetch_messages(target, FetchOptions(count=10))
    assert len(page.messages) == 2
    assert page.messages[0].is_self is True
    assert page.messages[1].is_self is False


@pytest.mark.asyncio
async def test_welink_connector_fetch_messages_raises_on_cli_error(plugin_with_mock):
    plugin, mock = plugin_with_mock
    mock.enqueue_history("error detail", exit_code=2)
    target = ChannelTarget(kind="group", external_id="group-a")
    with pytest.raises(RuntimeError, match="welink-cli"):
        await plugin.fetch_messages(target, FetchOptions())


@pytest.mark.asyncio
async def test_welink_connector_send_message_to_group_success(plugin_with_mock):
    plugin, mock = plugin_with_mock
    mock.enqueue_send(ok=True)
    target = ChannelTarget(kind="group", external_id="group-a")
    result = await plugin.send_message(target, "hello reply")
    assert result.ok is True
    assert any("来自 alice 的数字分身：hello reply" in " ".join(args) for args, _ in mock.calls)


@pytest.mark.asyncio
async def test_welink_connector_send_message_rejects_empty_content(plugin_with_mock):
    plugin, _ = plugin_with_mock
    target = ChannelTarget(kind="group", external_id="group-a")
    result = await plugin.send_message(target, "   ")
    assert result.ok is False
    assert result.error_code == "empty_content"


def test_format_outgoing_reply_text_uses_account():
    assert format_outgoing_reply_text(
        "你好",
        account="w3alice",
        mention_sender=True,
        sender="bob",
        max_reply_chars=1000,
    ) == "@bob 来自 w3alice 的数字分身：你好"


@pytest.mark.asyncio
async def test_welink_connector_resolve_identity_parses_auth_and_search(plugin_with_mock):
    plugin, mock = plugin_with_mock
    mock.enqueue_auth_status("WeLink CLI\nUID: uid-abc\nLogin: active")
    mock.enqueue_search_persons(json.dumps({
        "search_cli_person": {
            "data": [
                {"w3account": "alice@account", "welinkId": "uid-abc", "name": "Alice"},
            ]
        }
    }))
    identity = await plugin.resolve_identity()
    assert identity is not None
    assert identity.account == "alice@account"


@pytest.mark.asyncio
async def test_welink_connector_test_connection_ok_when_help_succeeds(plugin_with_mock):
    plugin, mock = plugin_with_mock
    mock.enqueue_help(stdout="welink-cli 1.0.0")
    result = await plugin.test_connection()
    assert result.ok is True


@pytest.mark.asyncio
async def test_welink_connector_test_connection_fails_on_error(plugin_with_mock):
    plugin, mock = plugin_with_mock
    mock.enqueue("--help", CliResult(exit_code=1, stdout="", stderr="not found"))
    result = await plugin.test_connection()
    assert result.ok is False


@pytest.mark.asyncio
async def test_welink_connector_discover_conversations(plugin_with_mock):
    plugin, mock = plugin_with_mock
    mock.enqueue_recent_conversations(json.dumps({
        "conversation_info": [
            {"group_id": "g1", "group_name": "Release", "target_account": ""},
        ]
    }))
    rows = await plugin.discover_conversations(query_count=20)
    assert rows[0].kind == "group"
    assert rows[0].external_id == "g1"
