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
    joyai_action_helper,
    joyai_provider,
    settings,
    video_search,
    video_voice,
)
from jiuwenswarm.extensions.video_duplex.backend.qwen_omni_gateway import (
    QWEN_OMNI_PROXY_PATH,
    QwenOmniRealtimeConfig,
)
from jiuwenswarm.extensions.video_duplex.backend.qwen_omni_tools import qwen_omni_tools
from jiuwenswarm.extensions.video_duplex.backend.tasks.prompts import task_receipt_followup
from jiuwenswarm.extensions.video_duplex.backend.video_files import normalize_file_items
from jiuwenswarm.server.runtime.session.session_history import append_history_record
from jiuwenswarm.server.runtime.session.session_metadata import (
    get_session_metadata,
    update_session_metadata,
)
_ALLOWED_IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}
_LOG_WRITE_LOCK = threading.Lock()

_CONVERSATION_EVENT_KINDS = {
    "user",
    "assistant",
    "reasoning",
    "tool_call",
    "tool_result",
    "file",
}


_REALTIME_TELEMETRY_EVENT = re.compile(
    r"^(?:realtime|qwen|joyai|search_result|barge_in|task_trace)_[a-z0-9_]{1,80}$"
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
    "rate_limit_strikes", "frame_interval_ms", "request_kind", "audio_append_sequence",
    "image_append_sequence", "has_deferred_image", "response_id",
    "audio_level", "speech_threshold", "cancel_event_sent",
    "replacing_pending_prompt", "instruction_chars", "frame_time_range",
    "audio_sequence", "image_sequence", "error_type", "raw_event",
    "speech_probability",
    "queued_count", "result_kind", "brief_chars",
    "latency_ms", "inference_ms", "wait_ms", "dropped_chunks",
    "rpc_request_id", "action", "before_job_id", "queue_version", "sequence",
    "status", "queue_position", "stored_status", "stored_sequence",
    "stored_queue_version", "stored_queue_position", "output_chars", "item_id",
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


def _validate_joyai_result(result: Any) -> None:
    """Reject broken internal action results before using them for task control."""
    if (
        not isinstance(result, dict)
        or any(not isinstance(result.get(key), str) for key in ("decision", "response", "delegation"))
        or result.get("decision") not in {"silence", "response", "delegation"}
    ):
        raise ValueError("JoyAI returned an invalid action result")


def _is_allowed_image_data_url(value: str) -> bool:
    header, separator, _ = value.partition(",")
    if not separator or not header.lower().startswith("data:"):
        return False
    parts = header[5:].lower().split(";")
    return parts[0] in _ALLOWED_IMAGE_MIME_TYPES and "base64" in parts[1:]


def _video_live_mode() -> str:
    mode = os.environ.get("VIDEO_LIVE_MODE", "realtime").strip().casefold()
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
) -> video_search.VideoSearchManager:
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

    from .realtime_context import ContextRestoreExpired, RealtimeContextRestorer

    context_restorer = RealtimeContextRestorer(search_manager)

    async def _realtime_config(ws, req_id, params, session_id):
        del session_id
        try:
            restored = await context_restorer.prepare(ws, str((params or {}).get("search_session_id") or "").strip())
        except Exception as exc:
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="CONTEXT_RESTORE_FAILED")
            return
        context_payload = {"context_restore": restored} if restored else {}
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
                payload={"provider": "joyai", "model": model, "reply_language": settings.reply_language(),
                         **context_payload},
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
                "tools": qwen_omni_tools(queue_manager=True),
                "reply_language": settings.reply_language(),
                **context_payload,
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
        reply_language = joyai_provider.normalize_response_language(
            params.get("reply_language", settings.reply_language())
        )
        request_kind = str(params.get("request_kind") or "frame").strip().casefold()
        if (frame_data_url and not _is_allowed_image_data_url(frame_data_url)) or (
            not frame_data_url and request_kind != "user"
        ):
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
        restore_instructions = ""
        restore_token = params.get("restore_token")
        if restore_token:
            try:
                if request_kind != "user":
                    raise ValueError("Restored context is allowed only for user requests")
                restore_instructions = await context_restorer.instructions(ws, search_session_id, restore_token)
            except ContextRestoreExpired as exc:
                await channel.send_response(ws, req_id, ok=False, error=str(exc), code="CONTEXT_RESTORE_EXPIRED")
                return
            except Exception as exc:
                await channel.send_response(ws, req_id, ok=False, error=str(exc), code="CONTEXT_RESTORE_FAILED")
                return
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
            model_instruction = instruction
            if request_kind == "user":
                model_instruction = (
                    joyai_provider.ground_user_instruction(instruction, tool_context, reply_language, restore_instructions=restore_instructions)
                    if restore_instructions else joyai_provider.ground_user_instruction(instruction, tool_context, reply_language)
                )
            request_args = [frame_data_url, model_instruction, upstream_session_id]
            if frame_time_range:
                request_args.append(frame_time_range)
            protocol_invalid = False
            try:
                result = await joyai_provider.request_frame(*request_args)
            except joyai_provider.JoyAIActionProtocolError as exc:
                if request_kind != "user":
                    raise
                protocol_invalid = True
                result = {**exc.completion, "decision": "response", "response": "", "delegation": ""}
            if request_kind == "user" and result.get("decision") != "delegation":
                try:
                    owner, scope = await search_manager.scope(ws, {"search_session_id": search_session_id})
                    snapshot = search_manager.snapshot(owner, scope)
                    jobs = snapshot["jobs"]
                    active = [job for job in jobs if job["status"] not in {"completed", "failed", "cancelled"}]
                    recent = active[:8] + [job for job in jobs[-8:] if job not in active[:8]]
                    task_context = [{key: job.get(key) for key in (
                        "job_id", "title", "question", "status", "revision", "parent_id", "successor_id",
                    )} for job in recent]
                    for task in task_context:
                        task["question"] = str(task.get("question") or "")[:1000]
                    result = await joyai_action_helper.review_action(
                        instruction=instruction,
                        result=result,
                        context=json.dumps({"restored_history": restore_instructions,
                                            "confirmed_tool_results": tool_context}, ensure_ascii=False),
                        current_tasks=task_context,
                        reply_language=reply_language,
                        command_id=str(params.get("command_id") or req_id),
                        protocol_invalid=protocol_invalid,
                    )
                except Exception as exc:
                    # The original answer may promise an action that was never
                    # submitted. Withhold it if review cannot establish a result.
                    await asyncio.to_thread(_append_joyai_log, {
                        **request_log, "stage": "failed", "error_code": "JOYAI_ACTION_HELPER_ERROR",
                        "error_type": type(exc).__name__,
                    })
                    await channel.send_response(
                        ws, req_id, ok=False,
                        error="JoyAI action review failed; no new task was submitted. Retry or check the task list.",
                        code="JOYAI_ACTION_HELPER_ERROR",
                    )
                    return
            _validate_joyai_result(result)
        except Exception as exc:  # noqa: BLE001
            error = str(exc) or "JoyAI frame request failed"
            error_code = (
                "JOYAI_RATE_LIMIT"
                if isinstance(exc, joyai_provider.JoyAIRateLimitError)
                else "JOYAI_ERROR"
            )
            await asyncio.to_thread(
                _append_joyai_log,
                {
                    **request_log,
                    "stage": "failed",
                    "error": error,
                    "error_code": error_code,
                },
            )
            await channel.send_response(
                ws, req_id, ok=False, error=error, code=error_code
            )
            return

        # JoyAI has a delegation marker rather than native function calls. Only
        # explicit schema-validated JSON is a control. Reviewed proposals enter
        # the same validation and command deduplication path as native calls.
        scheduling = None
        task_title = ""
        operation_count = 0
        queue_management = None
        search_job = None
        tools_used: list[str] = []
        while operation_count < 3:
            delegation = str(result.get("delegation") or "").strip()
            if result.get("decision") != "delegation" or not delegation.startswith("{"):
                break
            try:
                from .qwen_omni_tools import parse_qwen_omni_tool_call

                value = json.loads(delegation)
                if not isinstance(value, dict) or set(value) != {"name", "arguments"}:
                    raise ValueError("Invalid task operation")
                if value.get("name") not in {
                    "jiuwen_delegate",
                    "jiuwen_queue_manage",
                    "jiuwen_task_query",
                    "jiuwen_task_modify",
                    "jiuwen_task_cancel",
                }:
                    if value.get("name") == "jiuwen_task_answer":
                        raise ValueError("JoyAI voice form answers are disabled; complete the on-screen form")
                    raise ValueError("Unsupported task operation")
                owner, scope = await search_manager.scope(
                    ws, {"search_session_id": search_session_id}
                )
                call = parse_qwen_omni_tool_call(
                    {
                        **value,
                        "call_id": f"{params.get('command_id') or req_id}:{operation_count}",
                    }
                )
                if call.name == "jiuwen_queue_manage":
                    if request_kind != "user":
                        raise ValueError("Queue management requires an explicit user request")
                    management_instruction = question or instruction
                    queue_management = await search_manager.manage_queue_call(
                        owner, scope, call, management_instruction,
                        allow_answers=False, reply_language=reply_language,
                    )
                    tools_used.append(call.name)
                    response_text = queue_management.get("response", "")
                    if queue_management.get("state") == "incomplete":
                        response_text = (
                            "Queue management did not finish. Check the task list and scheduled jobs for any changes already applied."
                            if reply_language == "en" or (
                                reply_language == "match" and not re.search(r"[\u4e00-\u9fff]", question or instruction)
                            ) else "队列管理未全部完成，请查看任务列表和定时任务确认已执行的操作。"
                        )
                    # Only the manager's receipt-based answer reaches chat/TTS.
                    # Never speak JoyAI's unconfirmed acknowledgement or ask it
                    # to issue another operation for the same request.
                    result = {**result, "decision": "response", "response": response_text, "delegation": ""}
                    break
                if call.name == "jiuwen_delegate":
                    task_title = call.arguments.get("title", "")
                    scheduling = {
                        k: call.arguments.get(k)
                        for k in ("independent", "depends_on", "resources")
                        if k in call.arguments
                    }
                    result = {**result, "delegation": call.task}
                    break
                receipt = await search_manager.operate(owner, scope, call, user_instruction=question or instruction)
                operation_count += 1
                # Feed facts through the same provider conversation, including ids
                # needed for query -> modify. A receipt cannot manufacture a task.
                followup = model_instruction + "\n\n" + task_receipt_followup(receipt)
                result = await joyai_provider.request_frame(
                    frame_data_url, followup, upstream_session_id
                )
                _validate_joyai_result(result)
            except Exception as exc:
                await channel.send_response(
                    ws, req_id, ok=False, error=str(exc), code="TASK_REQUEST_REJECTED"
                )
                return
        if operation_count == 3 and result.get("decision") == "delegation":
            await channel.send_response(
                ws, req_id, ok=False,
                error="Task operation limit reached; query current state",
                code="TASK_REQUEST_REJECTED",
            )
            return

        delegation = str(result.get("delegation") or "").strip()
        if result.get("decision") == "delegation" and delegation:
            try:
                search_job = await search_manager.start(
                    ws,
                    question=question or delegation,
                    query=delegation,
                    title=task_title,
                    search_session_id=search_session_id,
                    visual_context=str(result.get("response") or ""),
                    frame_data_url=frame_data_url,
                    command_id=str(params.get("command_id") or req_id),
                    **({"scheduling": scheduling} if scheduling is not None else {}),
                )
            except ValueError as exc:
                await channel.send_response(
                    ws, req_id, ok=False, error=str(exc), code="TASK_REQUEST_REJECTED"
                )
                return
            tools_used.append("jiuwen_delegate")
            if result.get("action_helper_delegated"):
                result["response"] = joyai_action_helper.submission_response(search_job, reply_language, instruction)
        await asyncio.to_thread(
            _append_joyai_log,
            {
                **request_log,
                "stage": "completed",
                "decision": result.get("decision"),
                "response": result.get("response"),
                "delegation": result.get("delegation"),
                "raw_content": result.get("raw_content"),
                "model_raw_content": result.get("model_raw_content"),
                "latency_ms": result.get("latency_ms"),
                "timing": result.get("timing"),
                "tools_used": tools_used,
                "search_job": search_job,
                **({"queue_management": queue_management} if queue_management is not None else {}),
            },
        )
        await channel.send_response(
            ws,
            req_id,
            ok=True,
            payload={
                "response": result.get("response"),
                "search_job": search_job,
                **({"queue_management": queue_management} if queue_management is not None else {}),
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
        allowed: dict[str, Any] = {}
        for key, value in params.items():
            if key not in _REALTIME_TELEMETRY_FIELDS:
                continue
            if isinstance(value, (str, int, float, bool)):
                allowed[key] = value
        if event_name == "qwen_tool_call_invalid":
            arguments = params.get("arguments")
            if isinstance(arguments, dict):
                arguments = json.dumps(arguments, ensure_ascii=False)
            if isinstance(arguments, str):
                allowed["arguments"] = arguments[:2_000]
        for key in ("question", "query", "text", "transcript"):
            if isinstance(allowed.get(key), str):
                allowed[key] = allowed[key][:1_000]
        for key in ("result", "realtime_answer"):
            if isinstance(allowed.get(key), str):
                allowed[key] = allowed[key][:50_000]
        if event_name.startswith(("task_trace_", "qwen_tool_")) or event_name in {
            "qwen_realtime_error", "qwen_text_input_dispatched",
        }:
            from .tasks.diagnostics import trace_task

            if event_name.startswith("task_trace_") and isinstance(params.get("jobs"), list):
                allowed["jobs"] = [
                    {key: job.get(key) for key in (
                        "job_id", "status", "sequence", "queue_position", "queue_version",
                    ) if isinstance(job.get(key), (str, int, float, bool))}
                    for job in params["jobs"][:100] if isinstance(job, dict)
                ]
            trace_task(event_name, **{key: value for key, value in allowed.items() if key != "event"})
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
            if params.get("presentation") == "tool_result":
                extra = {"presentation": "tool_result"}
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
        elif kind == "file":
            files = normalize_file_items(params.get("files"))
            if not files:
                await channel.send_response(
                    ws, req_id, ok=False, code="INVALID_FILES",
                    message="files must contain named file resources.",
                )
                return
            event_type = "chat.file"
            extra = {"files": files}
            content = ""

        previous_title: str | None = None
        metadata = get_session_metadata(visible_session_id, cache_bust=True)
        if role == "user" and content.strip():
            # Refresh before history writes so a rename by AgentServer is respected.
            previous_title = str(metadata.get("title") or "")
            if previous_title == "Full-duplex conversation":
                # Older plugin versions persisted this placeholder as a real title.
                # Reuse Jiuwen's auto-title policy when that conversation resumes.
                update_session_metadata(
                    session_id=visible_session_id,
                    clear_title=True,
                    user_content=content,
                    touch_last_message_at=False,
                    sync_write=True,
                )

        append_history_record(
            session_id=visible_session_id,
            request_id=f"video-duplex-{event_id}",
            channel_id="video_duplex",
            role=role,
            content=content,
            timestamp=timestamp,
            event_type=event_type,
            extra=extra,
            mode=str(metadata.get("mode") or ("team.design" if metadata.get("work_mode") == "design" else "agent")),
        )
        if previous_title is not None:
            title = str(get_session_metadata(visible_session_id).get("title") or "")
            if title and title != previous_title:
                # The native listener updates both the header and workspace sidebar.
                await channel.send_event(ws, "session.updated", {
                    "session_id": visible_session_id,
                    "title": title,
                    "display_title": title,
                })
        await channel.send_response(
            ws,
            req_id,
            ok=True,
            payload={"persisted": True, "event_id": event_id},
        )

    async def _core_receipts(ws, req_id, params, session_id):
        from .task_adapter import task_identity, task_agent_query
        from jiuwenswarm.common.schema.message import ReqMethod

        try:
            owner, scope = task_identity(ws, str((params or {}).get("search_session_id") or ""))
            if not scope.startswith("task-duplex:"):
                raise ValueError("Core receipts require a saved conversation")
            payload = await task_agent_query(agent_client, ReqMethod.VOICE_SESSION_RECEIPTS,
                {"token": params.get("token"), "action": params.get("action")},
                scope.removeprefix("task-duplex:"), owner)
            await channel.send_response(ws, req_id, ok=True, payload=payload)
        except Exception as exc:
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="CORE_RECEIPT_REJECTED")

    handlers = {
        "video.realtime.config": _realtime_config,
        "video.core.subscribe": _core_receipts,
        "video.joyai.frame": _joyai_frame,
        "video.realtime.telemetry": _realtime_telemetry,
        "video.conversation.append": _append_conversation,
        **voice_handlers,
        "video.qwen.tool": search_manager.handle_qwen_tool,
        "video.search.status": search_manager.handle_status,
        "video.search.answer": search_manager.handle_answer,
        "video.search.list": search_manager.handle_list,
        "video.search.control": search_manager.handle_control,
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

    return search_manager
