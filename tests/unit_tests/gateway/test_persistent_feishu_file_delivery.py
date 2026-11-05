"""Exercise Gateway file dispatch against the real Feishu sender signature."""

import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_gateway.channel_manager.channel_manager import ChannelManager
from agent_gateway.channel_manager.im_platforms.feishu.feishu_connect import FeishuChannel, FeishuConfig


@pytest.mark.asyncio
@pytest.mark.parametrize("share_mode", ["attachment", "upload"])
@pytest.mark.parametrize("uploaded", [False, True])
async def test_gateway_feishu_file_reaches_real_sender(tmp_path, monkeypatch, share_mode, uploaded):
    from agent_gateway.channel_manager import persistent_output
    monkeypatch.setattr(persistent_output, "get_user_workspace_dir", lambda: tmp_path)
    source = tmp_path / "jiuwenmate/workspaces/local/agent/sessions/session/output/report.txt"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"test123")
    channel = FeishuChannel(FeishuConfig(enabled=True, app_id="cli-test"), MagicMock())
    channel._api_client = MagicMock()
    channel._upload_file = MagicMock(return_value="file-test")
    channel._send_persistent_message = AsyncMock(return_value={
        "outcome": "delivered", "external_message_id": "om-test",
    })
    manager = ChannelManager(SimpleNamespace())
    manager.set_persistent_output_file_resolver(persistent_output.resolve_output_file)
    manager.register_channel(channel)
    file = {
        "key": "local/agent/sessions/session/output/report.txt", "name": "report.txt",
        "size": 7, "file_ref": "sha256:" + hashlib.sha256(b"test123").hexdigest(),
        "file_key": "file-test" if uploaded else None, "share_mode": share_mode,
    }
    result = await manager.deliver_persistent_result({
        "channel": "feishu", "app_id": "cli-test", "chat_id": "oc-test",
        "source_message_id": "om-source", "idempotency_key": "delivery-test", "file": file,
    })
    assert result["outcome"] == "delivered", result
    assert result["file_key"] == "file-test"
    channel._send_persistent_message.assert_awaited_once_with(
        "oc-test", "file", '{"file_key": "file-test"}', "om-source", "delivery-test",
    )
    if uploaded:
        channel._upload_file.assert_not_called()
    else:
        channel._upload_file.assert_called_once_with(str(source))
