"""ASR and TTS adapters shared by video-live providers."""

from __future__ import annotations

import asyncio
import base64
import os
import re
import time
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

import httpx

from jiuwenswarm.extensions.video_duplex.backend import joyai_provider


MAX_AUDIO_CHARS = 2_000_000
MAX_TTS_TEXT_CHARS = 800
_ALLOWED_AUDIO_MIME_TYPES = {
    "audio/webm",
    "audio/ogg",
    "audio/wav",
    "audio/mp4",
    "audio/mpeg",
}
_ASR_FILLER_ONLY = re.compile(
    r"^(?:(?:嗯|恩|呃|啊|哦|噢|唔|哼|额|诶|欸|哎|呀|唉|hmm|uhhuh|uh|um|ah|oh|ああ|ええ))+$",
    flags=re.IGNORECASE,
)


def is_allowed_audio_data_url(value: str) -> bool:
    header, separator, _ = value.partition(",")
    if not separator or not header.lower().startswith("data:"):
        return False
    parts = header[5:].lower().split(";")
    return parts[0] in _ALLOWED_AUDIO_MIME_TYPES and "base64" in parts[1:]


def tts_model_config() -> tuple[str, str, str, str]:
    """Read the dedicated TTS endpoint without guessing from other models."""
    return (
        (os.environ.get("VOICE_TTS_ENDPOINT") or os.environ.get("TTS_API_BASE") or "")
        .strip()
        .rstrip("/"),
        (os.environ.get("VOICE_API_KEY") or os.environ.get("TTS_API_KEY") or "").strip(),
        (os.environ.get("VOICE_TTS_MODEL") or os.environ.get("TTS_MODEL_NAME") or "").strip(),
        (os.environ.get("VOICE_TTS_VOICE") or os.environ.get("TTS_VOICE") or "").strip(),
    )


def asr_model_config() -> tuple[str, str, str]:
    """Read the normalized ASR endpoint, with legacy ASR_* fallback."""
    return (
        (os.environ.get("VOICE_ASR_ENDPOINT") or os.environ.get("ASR_API_BASE") or "")
        .strip()
        .rstrip("/"),
        (os.environ.get("VOICE_API_KEY") or os.environ.get("ASR_API_KEY") or "").strip(),
        (os.environ.get("VOICE_ASR_MODEL") or os.environ.get("ASR_MODEL_NAME") or "").strip(),
    )


def clean_model_text(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def is_ignorable_asr_filler(transcript: str) -> bool:
    """Return true only when ASR produced punctuation and filler sounds."""
    normalized = re.sub(r"[\W_]+", "", str(transcript or "").lower())
    return bool(normalized and _ASR_FILLER_ONLY.fullmatch(normalized))


def _asr_uses_transcription_endpoint(model: str) -> bool:
    mode = (
        os.environ.get("VOICE_ASR_MODE") or os.environ.get("ASR_API_MODE") or ""
    ).strip().casefold()
    if mode in {"transcription", "transcriptions", "audio"}:
        return True
    if mode in {"chat", "chat_completion", "chat_completions"}:
        return False
    normalized = model.casefold()
    return any(name in normalized for name in ("sensevoice", "whisper", "telespeechasr"))


def _openai_endpoint_base(endpoint: str, route: str) -> str:
    parsed = urlsplit(endpoint)
    path = parsed.path.rstrip("/")
    if not path.casefold().endswith(route.casefold()):
        return endpoint.rstrip("/")
    base_path = path[: -len(route)].rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, base_path, "", ""))


def resolve_asr_endpoint(endpoint: str, model: str) -> tuple[str, str]:
    """Resolve a full ASR endpoint to its request kind and SDK base URL."""
    path = urlsplit(endpoint).path.rstrip("/").casefold()
    transcription_route = "/audio/transcriptions"
    chat_route = "/chat/completions"
    if path.endswith(transcription_route):
        return "transcription", _openai_endpoint_base(endpoint, transcription_route)
    if path.endswith(chat_route):
        return "chat", _openai_endpoint_base(endpoint, chat_route)
    kind = "transcription" if _asr_uses_transcription_endpoint(model) else "chat"
    return kind, endpoint.rstrip("/")


def resolve_tts_endpoint(endpoint: str) -> str:
    """Accept a full speech endpoint while retaining legacy API-base compatibility."""
    normalized = endpoint.rstrip("/")
    if urlsplit(normalized).path.casefold().endswith("/audio/speech"):
        return normalized
    return f"{normalized}/audio/speech"


def _decode_audio_data_url(data_url: str, index: int) -> tuple[str, bytes, str]:
    header, separator, encoded = data_url.partition(",")
    if not separator or not is_allowed_audio_data_url(data_url):
        raise ValueError("audio data URL is invalid")
    mime_type = header[5:].partition(";")[0].casefold()
    suffix = {
        "audio/mpeg": ".mp3",
        "audio/mp4": ".m4a",
        "audio/ogg": ".ogg",
        "audio/webm": ".webm",
        "audio/wav": ".wav",
    }.get(mime_type, ".audio")
    try:
        audio = base64.b64decode(encoded, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise ValueError("audio base64 payload is invalid") from exc
    if not audio:
        raise ValueError("audio payload is empty")
    return f"microphone-{index}{suffix}", audio, mime_type


async def transcribe_audio(
    audio_inputs: list[tuple[str, str]],
    *,
    use_joyai_voice: bool,
    log_event: Callable[[dict[str, Any]], None] | None = None,
) -> str:
    if use_joyai_voice:
        if not audio_inputs:
            return ""
        transcripts = [
            await joyai_provider.transcribe_channel(data_url, log_event=log_event)
            for data_url, _ in audio_inputs
        ]
        return "。".join(text for text in transcripts if text)

    from openai import AsyncOpenAI

    endpoint, api_key, model = asr_model_config()
    if not audio_inputs or not all((endpoint, api_key, model)):
        return ""
    endpoint_kind, api_base = resolve_asr_endpoint(endpoint, model)
    client = AsyncOpenAI(api_key=api_key, base_url=api_base, timeout=45.0)
    try:
        if endpoint_kind == "transcription":
            transcripts: list[str] = []
            for index, (data_url, _) in enumerate(audio_inputs, start=1):
                filename, audio, mime_type = _decode_audio_data_url(data_url, index)
                response = await client.audio.transcriptions.create(
                    model=model,
                    file=(filename, audio, mime_type),
                )
                text = response if isinstance(response, str) else getattr(response, "text", "")
                cleaned = clean_model_text(str(text or ""))
                if cleaned:
                    transcripts.append(cleaned)
            return "。".join(transcripts)

        content = [
            {"type": "audio_url", "audio_url": {"url": data_url}}
            for data_url, _ in audio_inputs
        ]
        content.append({
            "type": "text",
            "text": "只转写清晰可辨的用户中文，不要解释；静音、噪声或听不清时返回空字符串，不得补写。",
        })
        response = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": content}],
            max_tokens=160,
            temperature=0,
        )
        return clean_model_text(response.choices[0].message.content or "")
    finally:
        await client.close()


async def synthesize_speech(text: str, *, use_joyai_voice: bool) -> tuple[bytes, str, str]:
    if use_joyai_voice:
        return await joyai_provider.synthesize_channel(text)

    endpoint, api_key, model, voice = tts_model_config()
    if not endpoint or not model:
        raise RuntimeError("请配置 VOICE_TTS_ENDPOINT 和 VOICE_TTS_MODEL")
    payload: dict[str, Any] = {
        "model": model,
        "input": text,
        "response_format": "mp3",
    }
    if voice:
        payload["voice"] = voice
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    async with httpx.AsyncClient(timeout=45.0) as client:
        response = await client.post(
            resolve_tts_endpoint(endpoint), headers=headers, json=payload
        )
    if response.status_code >= 400:
        raise RuntimeError(f"TTS 请求失败 ({response.status_code})")
    if not response.content:
        raise RuntimeError("TTS 没有返回音频")
    return response.content, "audio/mpeg", model


def create_voice_handlers(
    channel: Any,
    *,
    use_joyai_voice: Callable[[], bool],
    log_event: Callable[[dict[str, Any]], None],
    log_asr: Callable[[dict[str, Any]], None],
) -> dict[str, Callable[..., Any]]:
    """Build the video plugin's ASR/TTS RPC handlers."""
    stream_tasks: dict[tuple[int, str], asyncio.Task[None]] = {}

    async def tts_synthesize(ws, req_id, params, session_id):
        started_at = time.perf_counter()
        text = str(params.get("text") or "").strip() if isinstance(params, dict) else ""
        request_log = {
            "stage": "tts_requested",
            "request_id": str(req_id),
            "session_id": str(session_id or ""),
            "text_chars": len(text),
            "text_preview": text[:300],
        }
        await asyncio.to_thread(log_event, request_log)
        if not text or len(text) > MAX_TTS_TEXT_CHARS:
            await asyncio.to_thread(log_event, {
                **request_log,
                "stage": "tts_failed",
                "latency_ms": round((time.perf_counter() - started_at) * 1_000, 1),
                "error": f"text must contain 1-{MAX_TTS_TEXT_CHARS} characters",
            })
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error=f"text must contain 1-{MAX_TTS_TEXT_CHARS} characters",
                code="BAD_REQUEST",
            )
            return
        try:
            audio, mime, model = await synthesize_speech(
                text, use_joyai_voice=use_joyai_voice()
            )
        except Exception as exc:  # noqa: BLE001
            await asyncio.to_thread(log_event, {
                **request_log,
                "stage": "tts_failed",
                "latency_ms": round((time.perf_counter() - started_at) * 1_000, 1),
                "error": str(exc).strip() or "TTS failed",
            })
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error=str(exc).strip() or "TTS failed",
                code="TTS_ERROR",
            )
            return
        await asyncio.to_thread(log_event, {
            **request_log,
            "stage": "tts_completed",
            "latency_ms": round((time.perf_counter() - started_at) * 1_000, 1),
            "audio_bytes": len(audio),
            "audio_mime": mime,
            "model": model,
        })
        await channel.send_response(
            ws,
            req_id,
            ok=True,
            payload={
                "success": True,
                "audio_base64": base64.b64encode(audio).decode("ascii"),
                "audio_mime": mime,
                "model": model,
            },
        )

    async def run_tts_stream(
        ws,
        *,
        stream_id: str,
        text: str,
        request_id: str,
        session_id: str,
    ) -> None:
        started_at = time.perf_counter()
        first_chunk_ms: float | None = None
        sequence = 0
        stream_key = (id(ws), stream_id)

        async def emit_chunk(chunk: bytes) -> None:
            nonlocal first_chunk_ms, sequence
            sequence += 1
            elapsed_ms = round((time.perf_counter() - started_at) * 1_000, 1)
            if first_chunk_ms is None:
                first_chunk_ms = elapsed_ms
                await asyncio.to_thread(log_event, {
                    "stage": "tts_stream_first_chunk",
                    "request_id": request_id,
                    "session_id": session_id,
                    "stream_id": stream_id,
                    "text_chars": len(text),
                    "first_chunk_ms": first_chunk_ms,
                    "chunk_bytes": len(chunk),
                })
            await channel.send_event(ws, "video.tts.chunk", {
                "stream_id": stream_id,
                "sequence": sequence,
                "sample_rate": 24_000,
                "audio_base64": base64.b64encode(chunk).decode("ascii"),
            })

        try:
            audio_bytes, chunk_count = await joyai_provider.stream_channel_pcm(
                text, emit_chunk
            )
            latency_ms = round((time.perf_counter() - started_at) * 1_000, 1)
            await asyncio.to_thread(log_event, {
                "stage": "tts_stream_completed",
                "request_id": request_id,
                "session_id": session_id,
                "stream_id": stream_id,
                "text_chars": len(text),
                "latency_ms": latency_ms,
                "first_chunk_ms": first_chunk_ms,
                "audio_bytes": audio_bytes,
                "chunk_count": chunk_count,
                "model": "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
            })
            await channel.send_event(ws, "video.tts.done", {
                "stream_id": stream_id,
                "audio_bytes": audio_bytes,
                "chunk_count": chunk_count,
                "latency_ms": latency_ms,
                "first_chunk_ms": first_chunk_ms,
            })
        except asyncio.CancelledError:
            await asyncio.to_thread(log_event, {
                "stage": "tts_stream_cancelled",
                "request_id": request_id,
                "session_id": session_id,
                "stream_id": stream_id,
                "text_chars": len(text),
                "latency_ms": round((time.perf_counter() - started_at) * 1_000, 1),
                "first_chunk_ms": first_chunk_ms,
            })
            await channel.send_event(ws, "video.tts.cancelled", {"stream_id": stream_id})
        except Exception as exc:  # noqa: BLE001
            error = str(exc).strip() or "TTS stream failed"
            await asyncio.to_thread(log_event, {
                "stage": "tts_stream_failed",
                "request_id": request_id,
                "session_id": session_id,
                "stream_id": stream_id,
                "text_chars": len(text),
                "latency_ms": round((time.perf_counter() - started_at) * 1_000, 1),
                "first_chunk_ms": first_chunk_ms,
                "error": error,
            })
            await channel.send_event(ws, "video.tts.error", {
                "stream_id": stream_id,
                "error": error,
            })
        finally:
            current = asyncio.current_task()
            if stream_tasks.get(stream_key) is current:
                stream_tasks.pop(stream_key, None)

    async def tts_stream_start(ws, req_id, params, session_id):
        params = params if isinstance(params, dict) else {}
        text = str(params.get("text") or "").strip()
        stream_id = str(params.get("stream_id") or "").strip()
        if not text or len(text) > MAX_TTS_TEXT_CHARS:
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error=f"text must contain 1-{MAX_TTS_TEXT_CHARS} characters",
                code="BAD_REQUEST",
            )
            return
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", stream_id):
            await channel.send_response(
                ws, req_id, ok=False, error="stream_id is invalid", code="BAD_REQUEST"
            )
            return
        if not use_joyai_voice():
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="streaming TTS is unavailable for the configured voice provider",
                code="TTS_STREAM_UNAVAILABLE",
            )
            return
        stream_key = (id(ws), stream_id)
        existing = stream_tasks.get(stream_key)
        if existing is not None and not existing.done():
            await channel.send_response(
                ws, req_id, ok=False, error="stream_id is already active", code="CONFLICT"
            )
            return
        request_log = {
            "stage": "tts_stream_requested",
            "request_id": str(req_id),
            "session_id": str(session_id or ""),
            "stream_id": stream_id,
            "text_chars": len(text),
            "text_preview": text[:300],
        }
        await asyncio.to_thread(log_event, request_log)
        task = asyncio.create_task(run_tts_stream(
            ws,
            stream_id=stream_id,
            text=text,
            request_id=str(req_id),
            session_id=str(session_id or ""),
        ))
        stream_tasks[stream_key] = task
        await channel.send_response(
            ws,
            req_id,
            ok=True,
            payload={"started": True, "stream_id": stream_id, "sample_rate": 24_000},
        )

    async def tts_stream_cancel(ws, req_id, params, session_id):
        del session_id
        stream_id = (
            str(params.get("stream_id") or "").strip()
            if isinstance(params, dict)
            else ""
        )
        task = stream_tasks.get((id(ws), stream_id))
        cancelled = bool(task is not None and not task.done())
        if cancelled and task is not None:
            task.cancel()
        await channel.send_response(
            ws,
            req_id,
            ok=True,
            payload={"stream_id": stream_id, "cancelled": cancelled},
        )

    async def video_transcribe(ws, req_id, params, session_id):
        started_at = time.perf_counter()
        data_url = params.get("audio_data_url") if isinstance(params, dict) else None
        request_log: dict[str, Any] = {
            "request_id": str(req_id),
            "session_id": str(session_id or ""),
            "audio_chars": len(data_url) if isinstance(data_url, str) else 0,
            "audio_mime": (
                data_url[5:].partition(";")[0]
                if isinstance(data_url, str) and data_url.startswith("data:")
                else ""
            ),
        }
        if (
            not isinstance(data_url, str)
            or len(data_url) > MAX_AUDIO_CHARS
            or not is_allowed_audio_data_url(data_url)
        ):
            await asyncio.to_thread(log_asr, {
                **request_log,
                "outcome": "rejected",
                "transcript": "",
                "has_transcript": False,
                "latency_ms": round((time.perf_counter() - started_at) * 1000),
                "error": "audio_data_url is invalid",
            })
            await channel.send_response(
                ws, req_id, ok=False, error="audio_data_url is invalid", code="BAD_REQUEST"
            )
            return
        try:
            transcript = await transcribe_audio(
                [(data_url, "用户麦克风")],
                use_joyai_voice=use_joyai_voice(),
                log_event=log_asr,
            )
        except Exception as exc:  # noqa: BLE001
            error = str(exc) or "ASR failed"
            await asyncio.to_thread(log_asr, {
                **request_log,
                "outcome": "failed",
                "transcript": "",
                "has_transcript": False,
                "latency_ms": round((time.perf_counter() - started_at) * 1000),
                "error": error,
            })
            await channel.send_response(
                ws, req_id, ok=False, error=error, code="ASR_ERROR"
            )
            return
        raw_transcript = transcript
        ignored_filler = is_ignorable_asr_filler(raw_transcript)
        if ignored_filler:
            transcript = ""
        asr_event = {
            **request_log,
            "outcome": (
                "ignored_filler"
                if ignored_filler
                else ("completed" if transcript.strip() else "empty")
            ),
            "transcript": transcript,
            "has_transcript": bool(transcript.strip()),
            "latency_ms": round((time.perf_counter() - started_at) * 1000),
        }
        if ignored_filler:
            asr_event.update({
                "raw_transcript": raw_transcript,
                "ignored_reason": "filler_only",
            })
        await asyncio.to_thread(log_asr, asr_event)
        await asyncio.to_thread(log_event, {
            "stage": "asr_ignored_filler" if ignored_filler else "asr_completed",
            "request_id": str(req_id),
            "transcript": transcript,
            "has_transcript": bool(transcript.strip()),
            **(
                {"raw_transcript": raw_transcript, "ignored_reason": "filler_only"}
                if ignored_filler
                else {}
            ),
        })
        await channel.send_response(
            ws,
            req_id,
            ok=True,
            payload={
                "transcript": transcript,
                **({"ignored_reason": "filler_only"} if ignored_filler else {}),
            },
        )

    return {
        "video.transcribe": video_transcribe,
        "tts.synthesize": tts_synthesize,
        "tts.stream.start": tts_stream_start,
        "tts.stream.cancel": tts_stream_cancel,
    }
