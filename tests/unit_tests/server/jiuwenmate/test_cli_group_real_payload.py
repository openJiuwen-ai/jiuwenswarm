"""Use raw API timestamps instead of treating CLI display time as an authorization timestamp."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.server.jiuwenmate import cli_group_sharing as module


@pytest.mark.asyncio
async def test_cli_display_time_is_rehydrated_from_exact_raw_message():
    display = {"message_id": "om_one", "chat_id": "oc_one", "create_time": "2026-10-29 19:07",
               "content": "display", "sender": {"id": "ou_one", "name": "Alice"}}
    raw = {**display, "create_time": "1793272020811", "body": {"content": '{"text":"raw body"}'}}
    raw.pop("content")
    cli = SimpleNamespace(run_group_read=AsyncMock(return_value={"code": 0, "data": {"items": [raw]}}))
    rows = await module.verified_cli_rows(cli, "local", "agent", [display], "bot")
    message = module._message(rows[0])
    assert message.received_at.isoformat() == "2026-10-29T11:07:00.811000+00:00"
    assert message.text == "raw body"
    assert cli.run_group_read.await_args.args[2][:3] == ["api", "GET", "/open-apis/im/v1/messages/om_one"]


@pytest.mark.asyncio
async def test_raw_message_lookup_cannot_change_message_or_group():
    display = {"message_id": "om_one", "chat_id": "oc_one", "create_time": "2026-10-29 19:07"}
    cli = SimpleNamespace(run_group_read=AsyncMock(return_value={"code": 0, "data": {"items": [
        {**display, "chat_id": "oc_other", "create_time": "1793272020811"}]}}))
    with pytest.raises(module.UnsafeGroupRead):
        await module.verified_cli_rows(cli, "local", "agent", [display], "bot")


@pytest.mark.asyncio
async def test_valid_timestamp_does_not_make_mention_shortcut_a_raw_message():
    display = {'message_id': 'om_one', 'chat_id': 'oc_one', 'create_time': '1793272020811',
               'content': '@_user_1 hello', 'sender': {'id': 'display-id'}}
    raw = {**display, 'sender': {'id': {'open_id': 'ou_sender'}, 'sender_type': 'user'},
           'mentions': [{'key': '@_user_1', 'name': 'Bob', 'id': {'open_id': 'ou_bob'}}]}
    cli = SimpleNamespace(run_group_read=AsyncMock(return_value={'code': 0, 'data': {'items': [raw]}}))
    rows = await module.verified_cli_rows(cli, 'local', 'agent', [display], 'bot')
    message = module._message(rows[0])
    assert message.text == '@Bob hello' and message.sender_id == 'ou_sender'
    assert message.mention_ids == ('ou_bob',)
    assert cli.run_group_read.await_count == 1


@pytest.mark.asyncio
async def test_raw_mention_lookup_still_checks_group_identity():
    display = {'message_id': 'om_one', 'chat_id': 'oc_one', 'create_time': '1793272020811',
               'content': '@_user_1 hello'}
    raw = {**display, 'chat_id': 'oc_other'}
    cli = SimpleNamespace(run_group_read=AsyncMock(return_value={'code': 0, 'data': {'items': [raw]}}))
    with pytest.raises(module.UnsafeGroupRead):
        await module.verified_cli_rows(cli, 'local', 'agent', [display], 'bot')
