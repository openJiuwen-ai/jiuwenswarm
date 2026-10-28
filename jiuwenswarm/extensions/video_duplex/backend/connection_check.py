"""Check the saved provider connection without capturing microphone or video."""

import asyncio
import json
import os

import httpx
import websockets

from jiuwenswarm.extensions.video_duplex.backend.qwen_omni_gateway import QwenOmniRealtimeConfig
from jiuwenswarm.extensions.video_duplex.backend.settings import settings_payload


async def check_saved_connection():
    """Check Qwen session initialization or JoyAI model availability."""
    values = settings_payload(enabled=True)["values"]
    async with asyncio.timeout(15):
        if values["video_live_provider"] == "qwen_omni":
            config = QwenOmniRealtimeConfig.from_environment()
            async with websockets.connect(
                config.upstream_with_model(),
                additional_headers={"Authorization": f"Bearer {config.api_key}"},
                open_timeout=10,
                close_timeout=1,
            ) as socket:
                while True:
                    event = json.loads(await socket.recv())
                    if event.get("type") in {"session.created", "session.updated"}:
                        return
                    if event.get("type") == "error":
                        raise ValueError("The provider rejected the session. Check the model and API key.")
        else:
            base = values["joyai_api_base"].rstrip("/")
            model = values["joyai_model"]
            key = os.getenv("JOYAI_API_KEY", "").strip()
            if not base or not model or not key:
                raise ValueError("Configure the JoyAI endpoint, model and API key first.")
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(f"{base}/models", headers={"Authorization": f"Bearer {key}"})
                response.raise_for_status()
                available = response.json().get("data", [])
                if not any(item.get("id") == model for item in available):
                    raise ValueError("The configured model is not listed by the provider.")
