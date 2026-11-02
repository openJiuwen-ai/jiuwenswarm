"""WeLink notices must use a real sender and owner approvals retain unread state."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.gateway.channel_manager.base import BaseChannel
from jiuwenswarm.gateway.channel_manager.channel_manager import ChannelManager
from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_connect import WelinkChannel, WelinkConfig
from jiuwenswarm.server.jiuwenmate.web_conversations import WebConversationStore


@pytest.mark.asyncio
@pytest.mark.parametrize('chat_id,method', [('g', 'send_group_text'), ('p2p:human', 'send_direct_text')])
async def test_permission_notice_uses_real_welink_sender(tmp_path, chat_id, method):
    channel = WelinkChannel(WelinkConfig(account='h1', agent_id='a', assistant_credential_id='ak',
        credential_dir=str(tmp_path)), None)
    sender = AsyncMock(return_value={'outcome': 'unknown', 'error_code': 'AWAITING_CONFIRMATION'})
    setattr(channel, method, sender)
    manager = ChannelManager(AsyncMock())
    manager.register_channel(channel)
    result = await manager.deliver_thinking_notice({'channel': 'welink', 'app_id': 'ak',
        'chat_id': chat_id, 'text': '请打开 Web 处理审批', 'source_message_id': 'original',
        'idempotency_key': 'permission:123'})
    assert result['outcome'] == 'unknown'
    sender.assert_awaited_once()
    assert sender.call_args.kwargs['source_message_id'] == 'original'
    assert sender.call_args.kwargs['idempotency_key'] == 'permission:123'


@pytest.mark.asyncio
async def test_unimplemented_sender_never_silently_succeeds():
    with pytest.raises(NotImplementedError):
        await BaseChannel.send(SimpleNamespace(channel_id='unsupported'), SimpleNamespace(id='notice'))


def test_approval_unread_survives_restart_and_only_new_notice_reopens_it(tmp_path):
    path = tmp_path / 'state.sqlite3'
    now = datetime.now(timezone.utc)
    store = WebConversationStore(path)
    store.notify('local', 'a', 'sharing-approvals', text='跨群分享审批', now=now, notification_key='approval-1')
    assert WebConversationStore(path).list('local', 'a')[0].to_dict()['unread'] is True
    store.mark_read('local', 'a', 'sharing-approvals', through=now)
    store.notify('local', 'a', 'sharing-approvals', text='跨群分享审批', now=now + timedelta(seconds=1), notification_key='approval-1')
    assert store.list('local', 'a')[0].unread is False
    store.notify('local', 'a', 'sharing-approvals', text='跨群分享审批', now=now + timedelta(seconds=2), notification_key='approval-2')
    store.mark_read('local', 'a', 'sharing-approvals', through=now)
    assert store.list('local', 'a')[0].unread is True


def test_private_notice_keeps_original_reply_task_open():
    import json
    from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_assistant_link import AssistantLink, _QaSlot

    link = AssistantLink("ak", "sk")
    slot = _QaSlot(message_id="original", group_id="p2p:user", tool_session_id="task",
                   trace_id="trace", reply_message_id="reply", part_id="part", created_at=0, text="question", opened=True)
    link._slots["original"] = slot
    frames = []
    prepared = []
    def send(raw):
        assert prepared, 'receipt must be persisted before the first socket write'
        frames.append(json.loads(raw))
    socket = SimpleNamespace(send=send)
    link._send_text(socket, "p2p:user", "请打开 Web 审批", "original", finish_task=False,
                    on_prepared=lambda *ids: prepared.append(ids))
    assert link._slots["original"] is slot
    assert not any(frame.get("type") == "tool_done" for frame in frames)
    link._send_text(socket, "p2p:user", "最终结果", "original")
    assert "original" not in link._slots
    assert sum(frame.get("type") == "tool_done" for frame in frames) == 1
