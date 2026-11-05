# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Designer BGM generation on the ``MUSIC_GEN_*`` slot.

One instrumental bed per film, mixed onto the concatenated clips at compose.
``MUSIC_GEN_ENABLED`` is the Settings switch; nothing runs while it is off.
``MUSIC_GEN_PROTOCOL`` names the backend; without it an OpenRouter API URL
picks ``openrouter``. MiniMax cloud is not a music backend: MiniMax Music3 runs
on vLLM-Omni.

- ``vllm-omni``: a self-hosted vLLM-Omni server, ``POST {base}/audio/speech``.
  MiniMax Music3, YuE2 and MOSS-SoundEffect take different fields, so the
  served model name (or ``GET {base}/models``) picks the request shape.
- ``openrouter``: Google Lyria 3 on OpenRouter chat completions. Audio output
  only streams, as base64 MP3 pieces in ``choices[0].delta.audio.data``.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from jiuwenswarm.agents.harness.common.tools import gen_toolkits
from jiuwenswarm.agents.harness.common.tools.ssl_config import get_requests_verify

logger = logging.getLogger(__name__)

VLLM_OMNI = gen_toolkits.VLLM_OMNI
OPENROUTER = "openrouter"
MUSIC_BACKENDS = (VLLM_OMNI, OPENROUTER)

_ENV_PREFIX = "MUSIC_GEN"
_HTTP_TIMEOUT_SECONDS = 600.0
# A full Lyria 3 Pro song or a 9000-token Music3 render both fit under this.
_MUSIC_TOOL_TIMEOUT_SECONDS = 900.0

_INSTRUMENTAL_CLAUSE = "Instrumental only: no vocals, no singing, no lyrics."

# Music3 has no instrumental switch: ``input`` must be non-empty lyrics, so
# section tags without words stand in for an instrumental piece.
_MUSIC3_LYRICS = "[Intro]\n[Inst]\n[Outro]"
_MUSIC3_CAPTION_LIMIT = 20000
_MUSIC3_FPS = 25
_MUSIC3_MAX_TOKENS = 9000
# Music3 never ends on its own; the token budget cuts it off. Ask for more than the
# film so compose can pick the climax and the ending point from the extra material.
_MUSIC3_HEADROOM = 1.5

# YuE2 on vLLM-Omni: ``input`` is the lyrics (rejected when empty) and
# ``instructions`` the style caption; ``max_new_tokens`` counts 25 fps frames in
# [200, 9000]. The prompt and the frame budget share a 24576-token context.
_YUE2_LYRICS = "[instrumental]"
_YUE2_CAPTION_LIMIT = 4000
_YUE2_MIN_FRAMES = 200
_YUE2_MAX_FRAMES = 9000

_OPENROUTER_HOST = re.compile(r"(^|\.)openrouter\.ai$", re.IGNORECASE)
_OPENROUTER_DEFAULT_BASE = "https://openrouter.ai/api/v1"
_LYRIA_MODEL = "google/lyria-3-pro-preview"
# Lyria takes no length or instrumental field; both ride in the prompt.
_LYRIA_PROMPT_LIMIT = 4000
# Lyria rejects "no vocals, no singing, no lyrics" as PROHIBITED_CONTENT, so it
# gets the positive form.
_LYRIA_INSTRUMENTAL_CLAUSE = "A purely instrumental piece."
_OPENROUTER_STATUS_HINTS = {
    401: "API key rejected",
    402: "insufficient credits",
    403: "key disabled or spend limit reached",
    429: "rate limited",
}


@dataclass(frozen=True)
class MusicSlot:
    """The music slot as saved in the environment."""

    api_key: str
    api_base: str
    model: str
    backend: str | None
    enabled: bool = True

    @property
    def complete(self) -> bool:
        if not self.enabled:
            return False
        if self.backend == VLLM_OMNI:
            return bool(self.api_base)
        return self.backend is not None and bool(self.api_key)


@dataclass(frozen=True)
class MusicRequest:
    """One film-wide BGM bed."""

    caption: str
    duration_sec: float


@dataclass(frozen=True)
class _Audio:
    data: bytes
    suffix: str
    detail: str


class _MusicError(RuntimeError):
    pass


def music_slot() -> MusicSlot:
    api_base = os.environ.get(f"{_ENV_PREFIX}_API_BASE", "").strip()
    protocol = os.environ.get(f"{_ENV_PREFIX}_PROTOCOL", "").strip().lower()
    if protocol in MUSIC_BACKENDS:
        backend: str | None = protocol
    elif _OPENROUTER_HOST.search(urlparse(api_base).hostname or ""):
        backend = OPENROUTER
    else:
        backend = None
    return MusicSlot(
        api_key=os.environ.get(f"{_ENV_PREFIX}_API_KEY", "").strip(),
        api_base=api_base,
        model=os.environ.get(f"{_ENV_PREFIX}_MODEL_NAME", "").strip(),
        backend=backend,
        enabled=os.environ.get(f"{_ENV_PREFIX}_ENABLED", "").strip().lower() in ("1", "true", "yes", "on"),
    )


def music_problem() -> str | None:
    """Why Design cannot generate BGM, or None when it can."""
    slot = music_slot()
    if not slot.enabled:
        return "Music generation is switched off (MUSIC_GEN_ENABLED)."
    if slot.backend is None:
        return (
            "Music generation is not configured: set MUSIC_GEN_PROTOCOL "
            "(vllm-omni / openrouter) and MUSIC_GEN_API_BASE. MiniMax cloud is not "
            "a music backend; serve MiniMax Music3 on vLLM-Omni instead."
        )
    if not slot.complete:
        if slot.backend == VLLM_OMNI:
            return "Music generation on vLLM-Omni needs MUSIC_GEN_API_BASE."
        return f"Music generation on {slot.backend} needs MUSIC_GEN_API_KEY."
    return None


def music_tool_timeout_seconds() -> float:
    return _MUSIC_TOOL_TIMEOUT_SECONDS


async def generate_music(request: MusicRequest, *, dest_stem: Path) -> dict[str, str]:
    """Generate one bed; ``{"music_path", "detail"}`` or ``{"error"}``.

    ``dest_stem`` is the file path without suffix; the backend's format picks it.
    """
    problem = music_problem()
    if problem:
        return {"error": f"[ERROR]: {problem}"}
    slot = music_slot()
    logger.info(
        "Designer music generation backend=%s model=%s duration=%.1fs",
        slot.backend, slot.model, request.duration_sec,
    )
    try:
        async with _client() as client:
            if slot.backend == VLLM_OMNI:
                audio = await _vllm_omni_music(client, slot, request)
            else:
                audio = await _openrouter_music(client, slot, request)
    except _MusicError as exc:
        return {"error": f"[ERROR]: {exc}"}
    except httpx.HTTPError as exc:
        return {"error": f"[ERROR]: {slot.backend} music request failed: {type(exc).__name__}: {exc}"}
    if not audio.data:
        return {"error": f"[ERROR]: {audio.detail} returned an empty audio file."}
    path = Path(dest_stem).with_suffix(audio.suffix)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(audio.data)
    return {"music_path": str(path), "detail": audio.detail}


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS, verify=get_requests_verify())


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _error_text(response: httpx.Response) -> str:
    payload = _json(response)
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
        for key in ("detail", "message", "msg"):
            if payload.get(key):
                return str(payload[key])
    return response.text[:300]


def _instrumental_caption(caption: str, limit: int, clause: str = _INSTRUMENTAL_CLAUSE) -> str:
    room = max(0, limit - len(clause) - 1)
    return f"{caption.strip()[:room]}\n{clause}".strip()


async def _served_model(client: httpx.AsyncClient, base: str, api_key: str) -> str:
    response = await client.get(f"{base}/models", headers=_headers(api_key))
    if response.status_code != 200:
        hint = " (the address usually ends with /v1)" if response.status_code == 404 else ""
        raise _MusicError(
            f"vLLM-Omni at {base}: GET /models returned HTTP {response.status_code}{hint}; "
            "check MUSIC_GEN_API_BASE or set MUSIC_GEN_MODEL_NAME."
        )
    payload = _json(response)
    rows = payload.get("data") if isinstance(payload, dict) else None
    first = rows[0] if isinstance(rows, list) and rows else {}
    model = str((first.get("id") if isinstance(first, dict) else "") or "").strip()
    if not model:
        raise _MusicError(
            f"vLLM-Omni at {base} reported no served model; set MUSIC_GEN_MODEL_NAME."
        )
    return model


def _vllm_omni_body(model: str, request: MusicRequest) -> dict[str, Any]:
    name = model.lower()
    if "moss" in name and "sound" in name:
        return {
            "model": model,
            "input": "",
            "ambient_sound": _instrumental_caption(request.caption, _MUSIC3_CAPTION_LIMIT),
            "duration_seconds": round(request.duration_sec, 2),
            "response_format": "wav",
        }
    if "yue" in name:
        frames = round(request.duration_sec * _MUSIC3_HEADROOM * _MUSIC3_FPS)
        return {
            "model": model,
            "input": _YUE2_LYRICS,
            "instructions": _instrumental_caption(request.caption, _YUE2_CAPTION_LIMIT),
            "max_new_tokens": max(_YUE2_MIN_FRAMES, min(_YUE2_MAX_FRAMES, frames)),
            "response_format": "wav",
        }
    if "music" in name:
        tokens = max(1, min(_MUSIC3_MAX_TOKENS, round(request.duration_sec * _MUSIC3_HEADROOM * _MUSIC3_FPS)))
        return {
            "model": model,
            "input": _MUSIC3_LYRICS,
            "instructions": _instrumental_caption(request.caption, _MUSIC3_CAPTION_LIMIT),
            "max_new_tokens": tokens,
            "response_format": "wav",
        }
    raise _MusicError(
        f"vLLM-Omni model {model!r} is not a music model this tool drives "
        "(MiniMax Music3, YuE2 or MOSS-SoundEffect)."
    )


async def _vllm_omni_music(client: httpx.AsyncClient, slot: MusicSlot, request: MusicRequest) -> _Audio:
    base = slot.api_base.rstrip("/")
    model = slot.model or await _served_model(client, base, slot.api_key)
    body = _vllm_omni_body(model, request)
    response = await client.post(f"{base}/audio/speech", json=body, headers=_headers(slot.api_key))
    if response.status_code != 200:
        raise _MusicError(f"vLLM-Omni {model}: HTTP {response.status_code} {_error_text(response)}")
    if "json" in response.headers.get("content-type", "").lower():
        raise _MusicError(f"vLLM-Omni {model}: expected audio, got {_error_text(response)}")
    return _Audio(response.content, ".wav", f"vLLM-Omni {model}")


def _lyria_prompt(caption: str, seconds: float) -> str:
    length = f"Length: about {round(seconds)} seconds."
    body = _instrumental_caption(caption, _LYRIA_PROMPT_LIMIT - len(length) - 1, _LYRIA_INSTRUMENTAL_CLAUSE)
    return f"{body}\n{length}"


def _lyria_audio_pieces(chunk: dict[str, Any], model: str) -> tuple[list[bytes], str]:
    """(decoded audio pieces, finish_reason) from one stream chunk."""
    err = chunk.get("error")
    if isinstance(err, dict):
        raise _MusicError(f"OpenRouter {model}: {err.get('message') or err.get('code') or 'stream error'}")
    pieces: list[bytes] = []
    finish = ""
    for choice in chunk.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
        audio = delta.get("audio") if isinstance(delta.get("audio"), dict) else {}
        if audio.get("data"):
            # Each piece is its own base64 string; decode before joining.
            try:
                pieces.append(base64.b64decode(str(audio["data"])))
            except ValueError as exc:
                raise _MusicError(f"OpenRouter {model}: audio piece is not base64") from exc
        finish = str(choice.get("finish_reason") or finish)
    return pieces, finish


async def _openrouter_music(client: httpx.AsyncClient, slot: MusicSlot, request: MusicRequest) -> _Audio:
    base = (slot.api_base or _OPENROUTER_DEFAULT_BASE).rstrip("/")
    model = slot.model or _LYRIA_MODEL
    body = {
        "model": model,
        "modalities": ["text", "audio"],
        "stream": True,
        "messages": [{"role": "user", "content": _lyria_prompt(request.caption, request.duration_sec)}],
    }
    pieces: list[bytes] = []
    finish = ""
    async with client.stream(
        "POST", f"{base}/chat/completions", json=body, headers=_headers(slot.api_key)
    ) as response:
        if response.status_code != 200:
            await response.aread()
            hint = _OPENROUTER_STATUS_HINTS.get(response.status_code, "")
            detail = " ".join(part for part in (hint, _error_text(response)) if part)
            raise _MusicError(f"OpenRouter {model}: HTTP {response.status_code} {detail}".strip())
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except ValueError:
                continue
            if isinstance(chunk, dict):
                found, reason = _lyria_audio_pieces(chunk, model)
                pieces.extend(found)
                finish = reason or finish
    if not pieces:
        raise _MusicError(
            f"OpenRouter {model} streamed no audio (finish_reason={finish or 'none'}); "
            "the provider may have filtered the prompt"
        )
    return _Audio(b"".join(pieces), ".mp3", f"OpenRouter {model}")
