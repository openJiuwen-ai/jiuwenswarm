"""Core Agent execution and asynchronous search jobs for video-live sessions."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Awaitable, Callable
import uuid

from jiuwenswarm.extensions.video_duplex.backend.qwen_omni_tools import (
    parse_qwen_omni_tool_call,
)

logger = logging.getLogger(__name__)
VIDEO_TOOL_CHANNEL_ID = "video_tool"


_IMAGE_FILENAME_SUFFIXES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
MAX_FRAME_CHARS = 4_000_000


def _frame_media_item(frame_data_url: str) -> dict[str, str] | None:
    header, separator, encoded = frame_data_url.partition(",")
    if not separator or not header.lower().startswith("data:"):
        return None
    parts = header[5:].lower().split(";")
    mime_type = parts[0]
    suffix = _IMAGE_FILENAME_SUFFIXES.get(mime_type)
    if not suffix or "base64" not in parts[1:] or not encoded:
        return None
    return {
        "type": "image",
        "filename": f"video-search-frame{suffix}",
        "mimeType": mime_type,
        "base64Data": encoded,
    }


def core_agent_text(value: Any, *, limit: int = 280) -> str:
    if isinstance(value, str):
        text = value
    elif value is None:
        return ""
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            text = str(value)
    normalized = re.sub(r"\s+", " ", text).strip()
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[:limit].rstrip()}..."


def core_agent_progress(payload: dict[str, Any]) -> dict[str, Any] | None:
    event_type = str(payload.get("event_type") or "").strip()
    if event_type == "chat.reasoning":
        return {"stage": "reasoning", "title": "正在分析问题", "status": "running"}
    if event_type == "chat.tool_call":
        tool = payload.get("tool_call") if isinstance(payload.get("tool_call"), dict) else payload
        name = str(
            tool.get("display_name") or tool.get("name") or payload.get("tool_name") or "工具"
        ).strip()
        detail = core_agent_text(tool.get("formatted_args") or tool.get("arguments"))
        return {
            "stage": "tool_call",
            "title": f"调用工具：{name}",
            "detail": detail,
            "status": "running",
            "tool_call_id": str(tool.get("id") or tool.get("tool_call_id") or ""),
            "tool_name": name,
        }
    if event_type == "chat.tool_update":
        update = payload.get("tool_update") if isinstance(payload.get("tool_update"), dict) else payload
        name = str(update.get("tool_name") or update.get("name") or "工具").strip()
        detail = core_agent_text(update.get("beam_search") or update.get("progress"))
        return {
            "stage": "tool_update",
            "title": f"{name} 正在执行",
            "detail": detail,
            "status": "running",
            "tool_call_id": str(update.get("tool_call_id") or ""),
            "tool_name": name,
        }
    if event_type == "chat.tool_result":
        result = payload.get("tool_result") if isinstance(payload.get("tool_result"), dict) else payload
        name = str(result.get("tool_name") or result.get("name") or "工具").strip()
        raw_status = str(result.get("status") or "").strip().lower()
        failed = result.get("success") is False or raw_status in {
            "error", "failed", "failure", "timeout", "timed_out",
        }
        detail = core_agent_text(
            result.get("summary") or result.get("error") or result.get("result")
        )
        return {
            "stage": "tool_result",
            "title": f"{name}{'执行失败' if failed else '执行完成'}",
            "detail": detail,
            "status": "failed" if failed else "completed",
            "tool_call_id": str(result.get("tool_call_id") or ""),
            "tool_name": name,
        }
    if event_type == "todo.updated":
        todos = payload.get("todos")
        if not isinstance(todos, list) or not todos:
            return None
        completed = sum(
            1 for item in todos
            if isinstance(item, dict) and str(item.get("status") or "").lower() == "completed"
        )
        return {
            "stage": "plan",
            "title": "执行计划已更新",
            "detail": f"{completed}/{len(todos)} 项已完成",
            "status": "running",
        }
    if event_type == "chat.delta" and str(payload.get("content") or "").strip():
        return {"stage": "answer", "title": "正在整理搜索结果", "status": "running"}
    if event_type == "chat.error":
        return {
            "stage": "error",
            "title": "Core Agent 执行失败",
            "detail": core_agent_text(payload.get("error") or payload.get("content")),
            "status": "failed",
        }
    return None


async def execute_core_agent(
    agent_client: Any,
    *,
    question: str,
    query: str,
    visual_context: str,
    search_session_id: str,
    frame_data_url: str = "",
    normalize_media_attachments: Callable[[dict[str, Any], str | None], None]
    | None = None,
    on_progress: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """Run one video research job through the standard, full Core Agent API."""
    from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
    from jiuwenswarm.common.schema.message import ReqMethod

    client = agent_client.get("value") if isinstance(agent_client, dict) else agent_client
    if client is None:
        raise RuntimeError("AgentServer client is unavailable")
    request_id = f"video-core-{uuid.uuid4().hex}"
    core_session_id = f"video-tool-{uuid.uuid4().hex}"
    prompt = (
        f"用户问题：{question or query}\n"
        f"建议搜索线索：{query or question}\n"
        f"Realtime视觉模型提供的画面线索：{visual_context or '无'}\n\n"
        "请使用可用工具完成任务。请求附带当前视频帧时，如果问题涉及画面中的实体、文字或指代，"
        "先使用图片理解工具核对画面，再生成准确搜索词；外部事实必须以搜索和网页正文为依据。\n\n"
        "最终回答要求：必须使用简体中文。完成工具调用后直接回答用户问题，只保留结论、必要依据和必要来源，"
        "不得复述搜索、抓取、重试或核实过程。"
        "通常使用2至4个完整句子且不超过500个汉字；问题确实需要列举时可使用简短列表。"
    )
    params: dict[str, Any] = {
        "query": prompt,
        "content": prompt,
        "mode": "agent",
        "work_mode": "work",
        "source": "video_tool",
        "log_as_user": False,
        "video_question": question,
        "video_query": query,
        "video_visual_context": visual_context,
        "search_session_id": search_session_id,
    }
    media_item = _frame_media_item(frame_data_url)
    if media_item is not None:
        params["media_items"] = [media_item]
        if normalize_media_attachments is None:
            raise RuntimeError("Core media attachment service is unavailable")
        normalize_media_attachments(params, core_session_id)
    env = e2a_from_agent_fields(
        request_id=request_id,
        channel_id=VIDEO_TOOL_CHANNEL_ID,
        session_id=core_session_id,
        req_method=ReqMethod.CHAT_SEND,
        params=params,
        is_stream=False,
        timestamp=time.time(),
    )
    send_stream = getattr(client, "send_request_stream", None)
    if not callable(send_stream):
        response = await client.send_request(env)
        payload = response.payload if isinstance(response.payload, dict) else {}
        if not response.ok:
            raise RuntimeError(str(payload.get("error") or "Jiuwen Core Agent failed"))
        answer = str(payload.get("content") or payload.get("answer") or "").strip()
        if not answer:
            raise RuntimeError("Jiuwen Core Agent returned empty output")
        return {**payload, "answer": answer, "raw_answer_chars": len(answer)}

    final_payload: dict[str, Any] = {}
    delta_parts: list[str] = []
    tools_used: list[str] = []
    emitted_once: set[str] = set()
    async for chunk in send_stream(env):
        payload = chunk.payload if isinstance(chunk.payload, dict) else {}
        event_type = str(payload.get("event_type") or "").strip()
        if event_type == "chat.error":
            raise RuntimeError(
                str(payload.get("error") or payload.get("content") or "Jiuwen Core Agent failed")
            )
        content = str(payload.get("content") or "")
        if event_type == "chat.delta" and content:
            delta_parts.append(content)
        elif event_type == "chat.final":
            final_payload = payload
        progress = core_agent_progress(payload)
        if progress is None:
            continue
        stage = str(progress.get("stage") or "")
        tool_key = str(progress.get("tool_call_id") or "")
        dedupe_key = f"{stage}:{tool_key}" if tool_key else stage
        if stage in {"reasoning", "answer", "plan"} and dedupe_key in emitted_once:
            continue
        emitted_once.add(dedupe_key)
        tool_name = str(progress.get("tool_name") or "").strip()
        if tool_name and tool_name not in tools_used:
            tools_used.append(tool_name)
        if on_progress is not None:
            await on_progress(progress)

    answer = str(final_payload.get("content") or "").strip() or "".join(delta_parts).strip()
    if not answer:
        raise RuntimeError("Jiuwen Core Agent returned empty output")
    return {
        **final_payload,
        "answer": answer,
        "raw_answer_chars": len(answer),
        "tools_used": tools_used,
    }


class VideoSearchManager:
    """Own search-job lifecycle and recovery state for one video RPC registry."""

    def __init__(
        self,
        channel: Any,
        agent_client: Any,
        *,
        normalize_media_attachments: Callable[
            [dict[str, Any], str | None], None
        ]
        | None = None,
        log_event: Callable[[dict[str, Any]], None],
        qwen_active: Callable[[], bool],
        max_concurrency: int = 2,
        max_cached_jobs: int = 128,
    ) -> None:
        self._channel = channel
        self._agent_client = agent_client
        self._normalize_media_attachments = normalize_media_attachments
        self._log_event = log_event
        self._qwen_active = qwen_active
        self._jobs: dict[str, dict[str, Any]] = {}
        self._tasks: set[asyncio.Task] = set()
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._max_cached_jobs = max_cached_jobs

    async def _send_event(self, ws: Any, event: str, payload: dict[str, Any]) -> None:
        try:
            await self._channel.send_event(ws, event, payload)
        except Exception:  # noqa: BLE001 - progress delivery is best-effort
            logger.debug("Failed to send video search event %s", event, exc_info=True)

    async def _run_job(
        self,
        ws: Any,
        *,
        job_id: str,
        search_session_id: str,
        question: str,
        query: str,
        visual_context: str,
        frame_data_url: str,
        tool_call_id: str = "",
        tool_name: str = "",
    ) -> None:
        started_at = time.perf_counter()
        progress_history: list[dict[str, Any]] = []
        base_payload = {
            "job_id": job_id,
            "search_session_id": search_session_id,
            "question": question,
            "query": query,
            "engine": "Jiuwen Core Agent",
            "has_frame": bool(frame_data_url),
            **({"tool_call_id": tool_call_id} if tool_call_id else {}),
            **({"tool_name": tool_name} if tool_name else {}),
        }

        async def emit_progress(progress: dict[str, Any]) -> None:
            entry = {
                **progress,
                "sequence": len(progress_history) + 1,
                "elapsed_ms": round((time.perf_counter() - started_at) * 1000),
            }
            progress_history.append(entry)
            current = self._jobs.get(job_id, {})
            self._jobs[job_id] = {
                **current,
                **base_payload,
                "status": "running",
                "progress_history": list(progress_history),
            }
            await self._send_event(ws, "video.search.progress", {
                **base_payload,
                "status": "running",
                "progress": entry,
            })

        start_progress = {
            "stage": "started",
            "title": "Core Agent 已开始处理",
            "status": "running",
            "sequence": 1,
            "elapsed_ms": 0,
        }
        progress_history.append(start_progress)
        self._jobs[job_id] = {
            **base_payload,
            "status": "running",
            "progress_history": list(progress_history),
        }
        await self._send_event(ws, "video.search.started", {
            **base_payload,
            "status": "running",
            "progress_history": list(progress_history),
        })
        await asyncio.to_thread(self._log_event, {"stage": "search_started", **base_payload})
        try:
            async with self._semaphore:
                core_result = await execute_core_agent(
                    self._agent_client,
                    question=question,
                    query=query,
                    visual_context=visual_context,
                    search_session_id=search_session_id,
                    frame_data_url=frame_data_url,
                    normalize_media_attachments=self._normalize_media_attachments,
                    on_progress=emit_progress,
                )
                answer = core_result["answer"]
                await asyncio.to_thread(self._log_event, {
                    "stage": "core_agent_completed",
                    **base_payload,
                    "tools_used": core_result.get("tools_used", []),
                    "model": core_result.get("model", ""),
                    "answer_chars": len(answer),
                })
            if not answer:
                raise RuntimeError("Jiuwen Core Agent returned empty output")
            latency_ms = round((time.perf_counter() - started_at) * 1000)
            progress_history.append({
                "stage": "completed",
                "title": "Core Agent 已完成搜索",
                "status": "completed",
                "sequence": len(progress_history) + 1,
                "elapsed_ms": latency_ms,
            })
            completed_payload = {
                **base_payload,
                "status": "completed",
                "result": answer,
                "latency_ms": latency_ms,
                "progress_history": list(progress_history),
            }
            self._jobs[job_id] = completed_payload
            await asyncio.to_thread(
                self._log_event, {"stage": "search_completed", **completed_payload}
            )
            await self._send_event(ws, "video.search.completed", completed_payload)
        except Exception as exc:  # noqa: BLE001
            error = str(exc).strip() or "Jiuwen Core Agent failed"
            latency_ms = round((time.perf_counter() - started_at) * 1000)
            progress_history.append({
                "stage": "failed",
                "title": "Core Agent 执行失败",
                "detail": core_agent_text(error),
                "status": "failed",
                "sequence": len(progress_history) + 1,
                "elapsed_ms": latency_ms,
            })
            failed_payload = {
                **base_payload,
                "status": "failed",
                "error": error,
                "latency_ms": latency_ms,
                "progress_history": list(progress_history),
            }
            self._jobs[job_id] = failed_payload
            await asyncio.to_thread(
                self._log_event, {"stage": "search_failed", **failed_payload}
            )
            await self._send_event(ws, "video.search.failed", failed_payload)

    def start(
        self,
        ws: Any,
        *,
        question: str,
        query: str,
        search_session_id: str,
        visual_context: str = "",
        frame_data_url: str = "",
        tool_call_id: str = "",
        tool_name: str = "",
    ) -> dict[str, Any]:
        job_id = uuid.uuid4().hex
        search_job = {
            "id": job_id,
            "status": "running",
            "question": question,
            "query": query,
            "search_session_id": search_session_id,
            **({"tool_call_id": tool_call_id} if tool_call_id else {}),
            **({"tool_name": tool_name} if tool_name else {}),
        }
        if len(self._jobs) >= self._max_cached_jobs:
            self._jobs.pop(next(iter(self._jobs)))
        self._jobs[job_id] = {"job_id": job_id, **search_job}
        task = asyncio.create_task(self._run_job(
            ws,
            job_id=job_id,
            search_session_id=search_session_id,
            question=question,
            query=query,
            visual_context=visual_context,
            frame_data_url=frame_data_url,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
        ))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return search_job

    def find_running(self, *, query: str, search_session_id: str) -> dict[str, Any] | None:
        normalized_query = re.sub(r"\s+", " ", query).strip().casefold()
        if not normalized_query:
            return None
        for job in reversed(list(self._jobs.values())):
            if (
                job.get("status") == "running"
                and job.get("search_session_id") == search_session_id
                and re.sub(r"\s+", " ", str(job.get("query") or "")).strip().casefold()
                == normalized_query
            ):
                return {
                    "id": str(job.get("job_id") or ""),
                    "status": "running",
                    "question": str(job.get("question") or ""),
                    "query": str(job.get("query") or ""),
                    "search_session_id": search_session_id,
                    "reused": True,
                }
        return None

    async def handle_qwen_tool(
        self, ws: Any, req_id: Any, params: Any, session_id: Any
    ) -> None:
        del session_id
        raw_params = params if isinstance(params, dict) else {}
        question = str(raw_params.get("question") or "").strip()
        search_session_id = str(raw_params.get("search_session_id") or "").strip()
        frame_data_url = str(raw_params.get("frame_data_url") or "").strip()
        request_log = {
            "stage": "qwen_tool_requested",
            "request_id": str(req_id),
            "name": str(raw_params.get("name") or "").strip(),
            "call_id": str(raw_params.get("call_id") or "").strip(),
            "question": question,
            "search_session_id": search_session_id,
        }
        await asyncio.to_thread(self._log_event, request_log)
        try:
            if not self._qwen_active():
                raise ValueError("Qwen Omni Realtime is not the active video provider")
            tool_call = parse_qwen_omni_tool_call(raw_params)
            if not question:
                question = tool_call.query
            if len(question) > 500:
                raise ValueError("question must not exceed 500 characters")
            if not search_session_id or len(search_session_id) > 200:
                raise ValueError("search_session_id must contain 1-200 characters")
            if frame_data_url and (
                len(frame_data_url) > MAX_FRAME_CHARS
                or _frame_media_item(frame_data_url) is None
            ):
                raise ValueError("frame_data_url is invalid")
        except ValueError as exc:
            error = str(exc)
            await asyncio.to_thread(self._log_event, {
                **request_log,
                "stage": "qwen_tool_rejected",
                "error": error,
            })
            await self._channel.send_response(
                ws, req_id, ok=False, error=error, code="BAD_REQUEST"
            )
            return

        search_job = self.start(
            ws,
            question=question,
            query=tool_call.query,
            search_session_id=search_session_id,
            frame_data_url=frame_data_url,
            tool_call_id=tool_call.call_id,
            tool_name=tool_call.name,
        )
        await asyncio.to_thread(self._log_event, {
            **request_log,
            "stage": "qwen_tool_accepted",
            "query": tool_call.query,
            "job_id": search_job["id"],
        })
        await self._channel.send_response(
            ws,
            req_id,
            ok=True,
            payload={
                "name": tool_call.name,
                "call_id": tool_call.call_id,
                "search_job": search_job,
            },
        )

    async def handle_status(self, ws: Any, req_id: Any, params: Any, session_id: Any) -> None:
        del session_id
        job_id = str(params.get("job_id") or "").strip() if isinstance(params, dict) else ""
        search_session_id = (
            str(params.get("search_session_id") or "").strip()
            if isinstance(params, dict)
            else ""
        )
        job = self._jobs.get(job_id)
        if not job or (
            search_session_id and job.get("search_session_id") != search_session_id
        ):
            await self._channel.send_response(
                ws, req_id, ok=False, error="search job not found", code="NOT_FOUND"
            )
            return
        await self._channel.send_response(ws, req_id, ok=True, payload=job)
