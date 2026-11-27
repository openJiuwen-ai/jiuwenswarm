"""Check the saved provider connection without capturing microphone or video."""

import asyncio
import json
import os
import uuid

import websockets

from jiuwenswarm.extensions.video_duplex.backend import doubao_realtime, joyai_provider
from jiuwenswarm.extensions.video_duplex.backend.qwen_omni_gateway import QwenOmniRealtimeConfig, qwen_upstream_error
from jiuwenswarm.extensions.video_duplex.backend.settings import settings_payload


async def check_saved_connection():
    """Check realtime session initialization or a minimal JoyAI completion."""
    values = settings_payload(enabled=True)["values"]
    provider = values["video_live_provider"]
    async with asyncio.timeout(15):
        if provider in {"qwen_omni", "doubao"}:
            if provider == "doubao":
                key = doubao_realtime.api_key()
                url = doubao_realtime.upstream_url()
                headers = {"X-Api-Key": key}
                initialize = doubao_realtime.session_create()
                ready_event = "session.created"
            else:
                config = QwenOmniRealtimeConfig.from_environment()
                key = config.api_key
                url = config.upstream_with_model()
                headers = {"Authorization": f"Bearer {key}"}
                initialize = {
                    "type": "session.update",
                    "session": {"modalities": ["audio", "text"], "voice": config.voice},
                }
                ready_event = "session.updated"
            async with websockets.connect(
                url,
                additional_headers=headers,
                open_timeout=10,
                close_timeout=1,
            ) as socket:
                await socket.send(json.dumps(initialize))
                while True:
                    event = json.loads(await socket.recv())
                    error = qwen_upstream_error(event, key)
                    if error:
                        raise ValueError(f"The provider rejected the session: {error[1]}")
                    if event.get("type") == ready_event:
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
