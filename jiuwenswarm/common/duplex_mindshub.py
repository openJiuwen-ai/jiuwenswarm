"""One bounded OpenAI-compatible MindsHub chat request for duplex routing."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any

import httpx

from jiuwenswarm.common.duplex_router import (
    SYSTEM_PROMPT, ControlSnapshot, InboundMessage, env_secret, prompt_for, validate_decision,
)

DEFAULT_MODEL = "mindshub_air"
DEFAULT_API_BASE = "https://api.mindshub.ai/v1"
DEFAULT_TIMEOUT_SECONDS = 5.0


async def classify_mindshub(
    snapshot: ControlSnapshot,
    messages: tuple[InboundMessage, ...],
    *,
    model_name: str = DEFAULT_MODEL,
    settings: Mapping[str, Any] | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, str]:
    """Return a strictly validated action; let observe() handle errors and timeouts."""
    settings = settings or {}
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("MindsHub timeout_seconds must be finite and positive")
    key_env = settings.get("api_key_env", "MINDSHUB_API_KEY")
    api_key = env_secret(str(key_env))
    if not api_key:
        raise ValueError("MindsHub API key environment variable is empty or missing")
    api_base = settings.get("api_base", DEFAULT_API_BASE).rstrip("/")
    url = httpx.URL(api_base + "/chat/completions")
    if (url.scheme not in ("http", "https") or not url.host
            or url.userinfo or url.query or url.fragment):
        raise ValueError("invalid MindsHub api_base")
    request = {
        "model": model_name, "temperature": 0, "max_tokens": 64,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt_for(snapshot, messages)},
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False) as client:
            response = await client.post(
                url, headers={"Authorization": f"Bearer {api_key}"}, json=request)
            response.raise_for_status()
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise ValueError("MindsHub response content must be a JSON string")
            action = validate_decision(json.loads(content))
            return {"action": action}
    except httpx.TimeoutException as exc:
        raise TimeoutError("MindsHub request timed out") from exc
