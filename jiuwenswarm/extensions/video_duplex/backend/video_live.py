"""Jiuwen Web RPC for short-window realtime audio-video Q&A."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
import re
import threading
from pathlib import Path
from typing import Any
import uuid

from jiuwenswarm.extensions.video_duplex.backend import (
    joyai_provider,
    video_search,
    video_voice,
)
from jiuwenswarm.extensions.video_duplex.backend.qwen_omni_gateway import (
    QWEN_OMNI_PROXY_PATH,
    QwenOmniRealtimeConfig,
)
from jiuwenswarm.extensions.video_duplex.backend.qwen_omni_tools import qwen_omni_tools
from jiuwenswarm.server.runtime.session.session_history import append_history_record
_ALLOWED_IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}
_LOG_WRITE_LOCK = threading.Lock()

_CONVERSATION_EVENT_KINDS = {
    "user",
    "assistant",
    "reasoning",
    "tool_call",
    "tool_result",
}


_REALTIME_TELEMETRY_EVENT = re.compile(
    r"^(?:realtime|qwen|joyai|search_result|barge_in)_[a-z0-9_]{1,80}$"
)
_REALTIME_TELEMETRY_FIELDS = {
    "event", "client_time", "level", "peak_level", "threshold",
    "noise_floor", "speech_ms", "assistant_playing", "response_active", "reason",
    "source", "frame_count", "model", "provider", "url", "code", "message",
    "client_build", "job_id", "search_session_id", "question",
    "query", "result", "realtime_answer", "turn_id", "attempt",
    "decision", "response_chars", "context_job_count", "context_chars",
    "name", "call_id", "text", "transcript", "has_transcript",
    "speech_epoch", "tts_generation", "resumed_text_chars", "text_chars",
    "transcript_chars", "user_speech_active", "response_generation",
    "current_generation", "stream_id", "streamed", "cooldown_ms",
    "rate_limit_strikes", "request_kind", "audio_append_sequence",
    "image_append_sequence", "has_deferred_image", "response_id",
    "audio_level", "speech_threshold", "cancel_event_sent",
    "replacing_pending_prompt", "instruction_chars", "frame_time_range",
    "audio_sequence", "image_sequence", "error_type", "raw_event",
}


def _append_jsonl(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
    # Search completion and router responses can finish on different worker
    # threads. Serialize each append so one JSON object always occupies one line.
    with _LOG_WRITE_LOCK:
        with path.open("a", encoding="utf-8") as stream:
            stream.write(line)


def _append_realtime_telemetry(event: dict[str, Any]) -> None:
    path = Path.home() / ".jiuwenswarm" / "logs" / "realtime-interrupt.jsonl"
    _append_jsonl(path, event)


def _append_realtime_session_log(event: dict[str, Any]) -> None:
    path = Path.home() / ".jiuwenswarm" / "logs" / "realtime-session.jsonl"
    event = {"server_time": datetime.now(timezone.utc).isoformat(), **event}
    _append_jsonl(path, event)


def _append_qwen_realtime_error_log(event: dict[str, Any]) -> None:
    path = Path.home() / ".jiuwenswarm" / "logs" / "qwen-realtime-errors.jsonl"
    event = {"server_time": datetime.now(timezone.utc).isoformat(), **event}
    _append_jsonl(path, event)


def _append_video_event_log(event: dict[str, Any]) -> None:
    path = Path.home() / ".jiuwenswarm" / "logs" / "video-live-events.jsonl"
    event = {
        "server_time": datetime.now(timezone.utc).isoformat(),
        **event,
    }
    _append_jsonl(path, event)


def _append_asr_log(event: dict[str, Any]) -> None:
    path = Path.home() / ".jiuwenswarm" / "logs" / "asr-results.jsonl"
    record = {
        "server_time": datetime.now(timezone.utc).isoformat(),
        **event,
    }
    _append_jsonl(path, record)


def _append_joyai_log(event: dict[str, Any]) -> None:
    event = dict(event)
    model_raw_content = event.pop("model_raw_content", None)
    path = Path.home() / ".jiuwenswarm" / "logs" / "joyai-video.jsonl"
    record = {
        "server_time": datetime.now(timezone.utc).isoformat(),
        **event,
    }
    _append_jsonl(path, record)

    if event.get("stage") != "completed" or event.get("decision") == "silence":
        return
    raw_content = (
        model_raw_content
        if isinstance(model_raw_content, str)
        else str(event.get("raw_content") or "")
    )
    if not raw_content.strip():
        return
    raw_path = Path.home() / ".jiuwenswarm" / "logs" / "joyai-raw-content.jsonl"
    raw_record = {
        "server_time": datetime.now(timezone.utc).isoformat(),
        "request_id": event.get("request_id", ""),
        "joyai_session_id": event.get("joyai_session_id", ""),
        "instruction": event.get("instruction", ""),
        "request_kind": event.get("request_kind", ""),
        "frame_time_range": event.get("frame_time_range", ""),
        "decision": event.get("decision", ""),
        "raw_content": raw_content,
    }
    adapter_content = str(event.get("raw_content") or "")
    if adapter_content != raw_content:
        raw_record["adapter_content"] = adapter_content
    _append_jsonl(raw_path, raw_record)


def _is_allowed_image_data_url(value: str) -> bool:
    header, separator, _ = value.partition(",")
    if not separator or not header.lower().startswith("data:"):
        return False
    parts = header[5:].lower().split(";")
    return parts[0] in _ALLOWED_IMAGE_MIME_TYPES and "base64" in parts[1:]


def _video_live_mode() -> str:
    mode = os.environ.get("VIDEO_LIVE_MODE", "joyai").strip().casefold()
    return "realtime" if mode == "realtime" else "joyai"


def _uses_joyai_voice_channel() -> bool:
    """Return whether JoyAI mode should use its native ASR/TTS WebSockets."""
    return joyai_provider.uses_native_voice_channel(_video_live_mode())


def video_duplex_enabled() -> bool:
    return (os.getenv("VIDEO_DUPLEX_ENABLED") or "true").strip().casefold() not in {
        "0",
        "false",
        "no",
        "off",
    }


def register_video_live_handler(
    channel: Any,
    *,
    agent_client: Any = None,
    normalize_media_attachments: Any = None,
) -> None:
    search_manager = video_search.VideoSearchManager(
        channel,
        agent_client,
        normalize_media_attachments=normalize_media_attachments,
        # Resolve the logger at execution time so runtime/test overrides remain effective.
        log_event=lambda event: _append_video_event_log(event),
        qwen_active=lambda: _video_live_mode() == "realtime",
    )
    voice_handlers = video_voice.create_voice_handlers(
        channel,
        use_joyai_voice=lambda: _uses_joyai_voice_channel(),
        log_event=lambda event: _append_video_event_log(event),
        log_asr=lambda event: _append_asr_log(event),
    )

    async def _realtime_config(ws, req_id, params, session_id):
        del params, session_id
        if _video_live_mode() == "joyai":
            api_base, _, model = joyai_provider.model_config()
            if not api_base or not model:
                await channel.send_response(
                    ws, req_id, ok=False,
                    error="请配置 JOYAI_API_BASE 和 JOYAI_MODEL_NAME",
                    code="VIDEO_CONFIG_ERROR",
                )
                return
            await channel.send_response(
                ws, req_id, ok=True,
                payload={"provider": "joyai", "model": model},
            )
            return
        config = QwenOmniRealtimeConfig.from_environment()
        try:
            config.validate()
        except ValueError as exc:
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error=str(exc),
                code="VIDEO_CONFIG_ERROR",
            )
            return
        await channel.send_response(
            ws,
            req_id,
            ok=True,
            payload={
                "provider": "qwen_omni",
                "url": QWEN_OMNI_PROXY_PATH,
                "model": config.model,
                "voice": config.voice,
                "tools": qwen_omni_tools(),
            },
        )

    async def _joyai_frame(ws, req_id, params, session_id):
        params = params if isinstance(params, dict) else {}
        frame_data_url = str(params.get("frame_data_url") or "").strip()
        instruction = str(params.get("instruction") or "").strip()
        requested_session_id = str(params.get("joyai_session_id") or "").strip()
        search_session_id = str(params.get("search_session_id") or "").strip()
        question = str(params.get("question") or "").strip()
        tool_context = str(params.get("tool_context") or "").strip()
        frame_time_range = str(params.get("frame_time_range") or "").strip()
        request_kind = str(params.get("request_kind") or "frame").strip().casefold()
        if not _is_allowed_image_data_url(frame_data_url):
            await channel.send_response(
                ws, req_id, ok=False,
                error="frame_data_url must be a base64 JPEG, PNG, or WebP data URL",
                code="BAD_REQUEST",
            )
            return
        if len(frame_data_url) > joyai_provider.MAX_FRAME_CHARS:
            await channel.send_response(
                ws, req_id, ok=False, error="frame_data_url is too large", code="BAD_REQUEST"
            )
            return
        if request_kind not in {"user", "frame"}:
            await channel.send_response(
                ws, req_id, ok=False, error="request_kind is invalid", code="BAD_REQUEST"
            )
            return
        if (
            len(instruction) > joyai_provider.MAX_INSTRUCTION_CHARS
            or (not instruction and request_kind != "frame")
        ):
            await channel.send_response(
                ws, req_id, ok=False,
                error=(
                    "instruction may be empty only for frame-only requests; "
                    "otherwise it must contain 1-"
                    f"{joyai_provider.MAX_INSTRUCTION_CHARS} characters"
                ),
                code="BAD_REQUEST",
            )
            return
        if len(tool_context) > joyai_provider.MAX_TOOL_CONTEXT_CHARS:
            await channel.send_response(
                ws, req_id, ok=False,
                error=(
                    "tool_context must not exceed "
                    f"{joyai_provider.MAX_TOOL_CONTEXT_CHARS} characters"
                ),
                code="BAD_REQUEST",
            )
            return
        if tool_context and request_kind != "user":
            await channel.send_response(
                ws, req_id, ok=False,
                error="tool_context is allowed only for user requests",
                code="BAD_REQUEST",
            )
            return
        if len(requested_session_id) > 200:
            await channel.send_response(
                ws, req_id, ok=False, error="joyai_session_id is too long", code="BAD_REQUEST"
            )
            return
        if len(frame_time_range) > 100 or (
            frame_time_range
            and not re.fullmatch(
                r"\d+(?:\.\d+)? seconds ~ \d+(?:\.\d+)? seconds",
                frame_time_range,
            )
        ):
            await channel.send_response(
                ws, req_id, ok=False, error="frame_time_range is invalid", code="BAD_REQUEST"
            )
            return
        upstream_session_id = requested_session_id or str(session_id or f"joyai-{uuid.uuid4().hex}")
        search_session_id = search_session_id or upstream_session_id
        request_log = {
            "request_id": str(req_id),
            "joyai_session_id": upstream_session_id,
            "instruction": instruction,
            "request_kind": request_kind,
            "frame_only": not bool(instruction),
            "frame_chars": len(frame_data_url),
            "frame_time_range": frame_time_range,
            "tool_context_chars": len(tool_context),
        }
        await asyncio.to_thread(_append_joyai_log, {**request_log, "stage": "requested"})
        try:
            model_instruction = (
                joyai_provider.ground_user_instruction(instruction, tool_context)
                if request_kind == "user"
                else instruction
            )
            request_args = [frame_data_url, model_instruction, upstream_session_id]
            if frame_time_range:
                request_args.append(frame_time_range)
            result = await joyai_provider.request_frame(*request_args)
        except Exception as exc:  # noqa: BLE001
            error = str(exc) or "JoyAI frame request failed"
            error_code = (
                "JOYAI_RATE_LIMIT"
                if isinstance(exc, joyai_provider.JoyAIRateLimitError)
                else "JOYAI_ERROR"
            )
            await asyncio.to_thread(_append_joyai_log, {
                **request_log,
                "stage": "failed",
                "error": error,
                "error_code": error_code,
            })
            await channel.send_response(
                ws, req_id, ok=False, error=error, code=error_code
            )
            return

        search_job = None
        tools_used: list[str] = []
        delegation = str(result.get("delegation") or "").strip()[:500]
        if (
            result.get("decision") == "delegation"
            and delegation
        ):
            search_question = question[:500] or delegation
            search_job = search_manager.find_running(
                query=delegation,
                search_session_id=search_session_id,
            )
            if search_job is None:
                search_job = search_manager.start(
                    ws,
                    question=search_question,
                    query=delegation,
                    search_session_id=search_session_id,
                    visual_context=str(result.get("response") or ""),
                    frame_data_url=frame_data_url,
                )
            tools_used.append("jiuwen_research")
        await asyncio.to_thread(_append_joyai_log, {
            **request_log,
            "stage": "completed",
            "decision": result["decision"],
            "response": result["response"],
            "delegation": result["delegation"],
            "raw_content": result["raw_content"],
            "model_raw_content": result.get("model_raw_content"),
            "latency_ms": result["latency_ms"],
            "timing": result["timing"],
            "tools_used": tools_used,
            "search_job": search_job,
        })
        await channel.send_response(
            ws,
            req_id,
            ok=True,
            payload={
                "response": result["response"],
                "search_job": search_job,
            },
        )

    async def _realtime_telemetry(ws, req_id, params, session_id):
        del session_id
        event_name = str(params.get("event") or "").strip() if isinstance(params, dict) else ""
        if not _REALTIME_TELEMETRY_EVENT.fullmatch(event_name):
            await channel.send_response(
                ws, req_id, ok=False, error="unsupported telemetry event", code="BAD_REQUEST"
            )
            return
        allowed: dict[str, str | int | float | bool] = {}
        for key, value in params.items():
            if key not in _REALTIME_TELEMETRY_FIELDS:
                continue
            if isinstance(value, (str, int, float, bool)):
                allowed[key] = value
        for key in ("question", "query", "text", "transcript"):
            if isinstance(allowed.get(key), str):
                allowed[key] = allowed[key][:1_000]
        for key in ("result", "realtime_answer"):
            if isinstance(allowed.get(key), str):
                allowed[key] = allowed[key][:50_000]
        if event_name in {
            "realtime_answer_final",
            "search_result_received",
            "search_result_duplicate_ignored",
            "search_result_queued",
            "search_result_queue_failed",
            "search_result_dispatched",
            "search_result_answered",
            "search_result_response_interrupted",
            "search_result_response_empty",
        }:
            allowed["stage"] = event_name
            await asyncio.to_thread(_append_video_event_log, allowed)
        elif event_name.startswith("realtime_"):
            await asyncio.to_thread(_append_realtime_session_log, allowed)
        else:
            allowed["server_time"] = datetime.now(timezone.utc).isoformat()
            await asyncio.to_thread(_append_realtime_telemetry, allowed)
            if event_name == "qwen_realtime_error":
                await asyncio.to_thread(_append_qwen_realtime_error_log, allowed)
        await channel.send_response(ws, req_id, ok=True, payload={"logged": True})

    async def _append_conversation(ws, req_id, params, session_id):
        del session_id
        visible_session_id = str(params.get("session_id") or "").strip()
        event_id = str(params.get("event_id") or "").strip()
        kind = str(params.get("kind") or "").strip().lower()
        if not visible_session_id or visible_session_id == "new":
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                code="SESSION_NOT_READY",
                message="A persisted session is required before appending Full-duplex history.",
            )
            return
        if not event_id or len(event_id) > 160:
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                code="INVALID_EVENT_ID",
                message="event_id is required and must not exceed 160 characters.",
            )
            return
        if kind not in _CONVERSATION_EVENT_KINDS:
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                code="INVALID_EVENT_KIND",
                message="Unsupported Full-duplex conversation event kind.",
            )
            return

        raw_timestamp = params.get("timestamp")
        try:
            timestamp = float(raw_timestamp)
        except (TypeError, ValueError):
            timestamp = datetime.now(timezone.utc).timestamp()
        if timestamp <= 0:
            timestamp = datetime.now(timezone.utc).timestamp()

        content = str(params.get("content") or "")
        if len(content) > 100_000:
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                code="CONTENT_TOO_LARGE",
                message="Full-duplex history content exceeds 100000 characters.",
            )
            return

        role = "user" if kind == "user" else "assistant"
        event_type: str | None = None
        extra: dict[str, Any] | None = None
        if kind == "assistant":
            event_type = "chat.final"
        elif kind == "reasoning":
            event_type = "chat.reasoning"
            extra = {
                "reasoning_content": content,
                "reasoning_started_at": params.get("started_at"),
                "reasoning_updated_at": params.get("updated_at"),
            }
            content = ""
        elif kind == "tool_call":
            tool_call = params.get("tool_call")
            if not isinstance(tool_call, dict):
                await channel.send_response(
                    ws,
                    req_id,
                    ok=False,
                    code="INVALID_TOOL_CALL",
                    message="tool_call must be an object.",
                )
                return
            event_type = "chat.tool_call"
            extra = {"tool_call": tool_call}
            content = ""
        elif kind == "tool_result":
            tool_result = params.get("tool_result")
            if not isinstance(tool_result, dict):
                await channel.send_response(
                    ws,
                    req_id,
                    ok=False,
                    code="INVALID_TOOL_RESULT",
                    message="tool_result must be an object.",
                )
                return
            event_type = "chat.tool_result"
            extra = {"tool_result": tool_result}
            content = ""

        append_history_record(
            session_id=visible_session_id,
            request_id=f"video-duplex-{event_id}",
            channel_id="video_duplex",
            role=role,
            content=content,
            timestamp=timestamp,
            event_type=event_type,
            extra=extra,
            mode="agent",
        )
        await channel.send_response(
            ws,
            req_id,
            ok=True,
            payload={"persisted": True, "event_id": event_id},
        )

    handlers = {
        "video.realtime.config": _realtime_config,
        "video.joyai.frame": _joyai_frame,
        "video.realtime.telemetry": _realtime_telemetry,
        "video.conversation.append": _append_conversation,
        **voice_handlers,
        "video.qwen.tool": search_manager.handle_qwen_tool,
        "video.search.status": search_manager.handle_status,
    }
    for method, handler in handlers.items():
        channel.register_method(
            method,
            handler,
            local_only=True,
            available_when_disabled=True,
        )
    _append_video_event_log({
        "stage": "video_handlers_registered",
        "module_path": __file__,
        "search_status_enabled": True,
    })
