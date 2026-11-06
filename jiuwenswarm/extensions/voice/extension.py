"""Expose the saved TTS profile's explicit synthesis check through Gateway."""

from __future__ import annotations

import asyncio
import base64
from typing import Any

from agent_gateway.extensions.sdk import ApplicationPluginExtension, ApplicationPluginServices

from jiuwenswarm.common.voice.tts import TtsSession


async def test_tts_handler(channel: Any, ws: Any, req_id: Any, params: Any, session_id: Any) -> None:
    session = None
    try:
        async with asyncio.timeout(60):
            session = TtsSession()
            try:
                await session.start()
                result = await session.synthesize("this is working", session.config.voice)
            finally:
                await session.aclose()
        if not result.audio:
            raise ValueError("TTS returned no audio")
    except Exception as exc:
        message = str(exc).strip() or "TTS test failed or timed out"
        if session is not None and session.config.api_key:
            message = message.replace(session.config.api_key, "[redacted]")
        await channel.send_response(ws, req_id, ok=False, error=message, code="TTS_ERROR")
        return
    await channel.send_response(ws, req_id, ok=True, payload={
        "success": True,
        "audio_base64": base64.b64encode(result.audio).decode("ascii"),
        "audio_mime": result.audio_mime,
    })


class SharedVoiceApplicationPlugin(ApplicationPluginExtension):
    plugin_id = "shared-voice"

    async def initialize(self, config: Any) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    def bind_web_channel(self, channel: Any, services: ApplicationPluginServices) -> None:
        async def test_synthesis(ws: Any, req_id: Any, params: Any, session_id: Any) -> None:
            await test_tts_handler(channel, ws, req_id, params, session_id)

        channel.register_method("tts.test", test_synthesis, local_only=True, available_when_disabled=True)


async def register_extensions(registry: Any) -> list[SharedVoiceApplicationPlugin]:
    extension = SharedVoiceApplicationPlugin()
    registry.register_application_plugin(extension)
    return [extension]
