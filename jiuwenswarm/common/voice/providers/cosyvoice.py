"""CosyVoice HTTP speech protocol behind the general TTS session interface."""

from __future__ import annotations

import asyncio
import time

import httpx

from ..config import TtsConfig
from ..tts import MAX_AUDIO_BYTES, MAX_TTS_TEXT_CHARS, TtsAudio, TtsError


class CosyVoiceTtsSession:
    def __init__(self, config: TtsConfig):
        self.config = config
        self._client: httpx.AsyncClient | None = None
        self._closed = False
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        self.config.validate()
        if self._closed:
            raise TtsError("SESSION_CLOSED", "TTS session has closed")
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(45, connect=15), follow_redirects=False)

    async def aclose(self) -> None:
        self._closed = True
        if self._client is not None:
            await self._client.aclose()

    async def synthesize(self, text: str, voice: str) -> TtsAudio:
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= MAX_TTS_TEXT_CHARS:
            raise ValueError(f"TTS text must contain 1-{MAX_TTS_TEXT_CHARS} characters")
        self.config.validate_voice(voice)
        text = text.strip()
        async with self._lock:
            await self.start()
            started = time.perf_counter()
            audio = bytearray()
            first_audio_ms = 0.0
            try:
                async with asyncio.timeout(60):
                    async with self._client.stream(
                        "POST", self.config.endpoint,
                        headers={"Authorization": f"Bearer {self.config.api_key}"},
                        json={"model": self.config.model, "input": text, "voice": voice, "response_format": "mp3"},
                    ) as response:
                        if response.status_code != 200:
                            raise TtsError("PROVIDER_ERROR", f"CosyVoice2 TTS request failed (HTTP {response.status_code})")
                        async for chunk in response.aiter_bytes():
                            if len(audio) + len(chunk) > MAX_AUDIO_BYTES:
                                raise TtsError("AUDIO_TOO_LARGE", "CosyVoice2 audio exceeds its size limit")
                            if chunk and not audio:
                                first_audio_ms = (time.perf_counter() - started) * 1000
                            audio.extend(chunk)
            except (TimeoutError, httpx.TimeoutException):
                raise TtsError("TIMEOUT", "CosyVoice2 TTS timed out") from None
            except httpx.RequestError:
                raise TtsError("PROVIDER_ERROR", "Cannot connect to the configured TTS service") from None
            if not audio:
                raise TtsError("PROVIDER_ERROR", "CosyVoice2 returned no audio")
            # The dedicated API supplies no generated transcript or sample-rate
            # metadata. Do not claim either has been independently verified.
            return TtsAudio(
                bytes(audio), None, voice, self.config.model, first_audio_ms,
                (time.perf_counter() - started) * 1000, audio_mime="audio/mpeg", sample_rate=None,
            )
