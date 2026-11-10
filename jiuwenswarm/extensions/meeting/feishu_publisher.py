# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Publish text and native voice messages through a host-owned Feishu client.

The host supplies its existing authenticated Feishu client and file service.
TTS, Opus encoding, routing, and retry policy belong to the host.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
from typing import TYPE_CHECKING
from uuid import uuid4

from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

if TYPE_CHECKING:
    from lark_oapi import Client

    from agent_gateway.channel_manager.im_platforms.feishu.feishu_file_service import FeishuFileService


class FeishuMeetingPublisher:
    """Send group messages and return delivery IDs, not playback acknowledgements."""

    def __init__(self, client: Client, files: FeishuFileService) -> None:
        self._client = client
        self._files = files

    async def send_text(self, chat_id: str, text: str, *, message_uuid: str | None = None) -> str:
        """Send full reply text; reuse message_uuid when retrying this message."""
        if not text.strip():
            raise ValueError("Feishu text must not be blank")
        return await self._send(chat_id, "text", {"text": text}, message_uuid)

    async def send_audio(self, chat_id: str, audio: bytes, *, message_uuid: str | None = None) -> str:
        """Upload complete Ogg/Opus bytes and send them as a native voice message."""
        if not (audio.startswith(b"OggS") and b"OpusHead" in audio[:128]):
            raise ValueError("Feishu native audio requires Ogg/Opus-encoded bytes")
        with tempfile.TemporaryDirectory(prefix="feishu-meeting-") as directory:
            path = Path(directory) / "speech.opus"
            await asyncio.to_thread(path.write_bytes, audio)
            uploaded = await self._files.upload_file_resource(str(path))
        if not uploaded or not uploaded.get("file_key"):
            raise RuntimeError("Feishu voice-message upload failed")
        return await self._send(chat_id, "audio", {"file_key": uploaded["file_key"]}, message_uuid)

    async def _send(self, chat_id: str, kind: str, content: dict[str, str], message_uuid: str | None) -> str:
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type(kind)
                .content(json.dumps(content, ensure_ascii=False))
                .uuid(message_uuid or uuid4().hex)
                .build()
            )
            .build()
        )
        response = await asyncio.to_thread(self._client.im.v1.message.create, request)
        if not response.success():
            raise RuntimeError(f"Feishu {kind} send failed: {response.code} {response.msg}")
        if response.data is None or not response.data.message_id:
            raise RuntimeError("Feishu send succeeded without a message ID")
        return response.data.message_id
