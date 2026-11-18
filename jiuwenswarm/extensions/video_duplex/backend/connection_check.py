"""Check the saved provider connection without capturing microphone or video."""

import asyncio
import json
import os
import uuid

import websockets

from jiuwenswarm.extensions.video_duplex.backend import joyai_provider
from jiuwenswarm.extensions.video_duplex.backend.qwen_omni_gateway import QwenOmniRealtimeConfig, qwen_upstream_error
from jiuwenswarm.extensions.video_duplex.backend.settings import settings_payload


async def check_saved_connection():
    """Check the saved provider's session handshake or minimal completion."""
    values = settings_payload(enabled=True)["values"]
    async with asyncio.timeout(15):
        if values["video_live_provider"] == "vllm_omni_ws":
            from .duplex_session_bridge import tools
            from .tool_adapters.vllm_omni import VllmOmniConfig, VllmOmniToolAdapter

            config = VllmOmniConfig.from_environment()
            headers = {"Authorization": "Bearer " + config.api_key} if config.api_key else {}
            async with websockets.connect(config.endpoint(), additional_headers=headers, close_timeout=1) as socket:
                async def send(event: dict) -> None:
                    await socket.send(json.dumps(event))

                adapter = VllmOmniToolAdapter(send)
                await adapter.configure(tools(), config)
                async for raw in socket:
                    event = json.loads(raw)
                    if event.get("type") == "error":
                        raise ValueError("The backend rejected duplex configuration")
                    if event.get("type") == "session.created":
                        adapter.adopt(event["session"], tool_count=len(tools()))
                        await send({"type": "session.close"})
                        return
        elif values["video_live_provider"] == "qwen_omni":
            config = QwenOmniRealtimeConfig.from_environment()
            async with websockets.connect(
                config.upstream_with_model(),
                additional_headers={"Authorization": f"Bearer {config.api_key}"},
                open_timeout=10,
                close_timeout=1,
            ) as socket:
                await socket.send(json.dumps({
                    "type": "session.update",
                    "session": {"modalities": ["audio", "text"], "voice": config.voice},
                }))
                while True:
                    event = json.loads(await socket.recv())
                    error = qwen_upstream_error(event, config.api_key)
                    if error:
                        raise ValueError(f"The provider rejected the session: {error[1]}")
                    if event.get("type") == "session.updated":
                        return
        else:
            base = values["joyai_api_base"].rstrip("/")
            model = values["joyai_model"]
            key = os.getenv("JOYAI_API_KEY", "").strip()
            if not base or not model or not key:
                raise ValueError("Configure the JoyAI endpoint, model and API key first.")
            # Some JoyAI endpoints implement completions without a model list.
            # Reuse inference headers and validation, with an isolated session
            # so the probe cannot change the active conversation's context.
            await joyai_provider.request_completion(
                "",
                "Reply with OK.",
                f"connection-check-{uuid.uuid4().hex}",
                max_tokens=8,
            )
