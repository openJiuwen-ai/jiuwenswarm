"""Core Agent execution and asynchronous search jobs for video-live sessions."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import logging
import re
import time
from typing import Any, Awaitable, Callable
import uuid

from jiuwenswarm.extensions.video_duplex.backend.qwen_omni_tools import (
    parse_qwen_omni_tool_call,
)
from jiuwenswarm.extensions.video_duplex.backend.video_files import normalize_file_items

logger = logging.getLogger(__name__)
VIDEO_TOOL_CHANNEL_ID = "video_tool"


_IMAGE_FILENAME_SUFFIXES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
MAX_FRAME_CHARS = 4_000_000
MAX_ORIGINAL_INSTRUCTION_CHARS = 4_000
MAX_TURN_ID_CHARS = 200
MAX_DELEGATION_CONTEXT_ITEMS = 6
MAX_DELEGATION_CONTEXT_CHARS = 8_000
MAX_DELEGATION_RESULT_CHARS = 2_400
MAX_REALTIME_BRIEF_CHARS = 180


def _brief_markers(nonce: str) -> tuple[str, str]:
    return (
        f"[[JIUWEN_BRIEF_BEGIN:{nonce}]]",
        f"[[JIUWEN_BRIEF_END:{nonce}]]",
    )


def core_agent_brief_protocol(nonce: str) -> str:
    begin, end = _brief_markers(nonce)
    return (
        "\n\n语音回执协议（必须放在完整答案之后）：\n"
        f"{begin}\n"
        "另写一至两句自然、简短的简体中文回执，概括任务结果，供实时模型直接播报。"
        "不得包含代码、JSON、网址、Markdown链接或完整网页正文，也不得声称尚未完成。\n"
        f"{end}\n"
        "上述随机标记必须原样输出且只输出一次。"
    )


def _result_kind(question: str, answer: str, tools_used: list[str]) -> str:
    combined_tools = " ".join(tools_used).casefold()
    combined_text = f"{question}\n{answer}".casefold()
    if re.search(r"search|fetch|browser|网页|搜索", combined_tools):
        return "research"
    if "```" in answer or re.search(
        r"\b(?:python|typescript|javascript|java|c\+\+|sql)\b|代码|程序|函数",
        combined_text,
    ):
        return "code"
    if re.search(r"计算|求解|等于|方程|积分|概率|\d+\s*[-+*/^]\s*\d+", combined_text):
        return "calculation"
    if re.search(r"file|glob|grep|document|pdf|文件|文档", combined_tools):
        return "file"
    if re.search(r"bash|powershell|write|edit|computer|执行|写入", combined_tools):
        return "action"
    return "generic"


def _safe_brief(value: str) -> str:
    brief = value.strip()
    if not brief or len(brief) > MAX_REALTIME_BRIEF_CHARS:
        return ""
    if "\n" in brief or "\r" in brief:
        return ""
    if re.search(r"https?://|www\.|```|`[^`]+`|\[\[JIUWEN_", brief, re.IGNORECASE):
        return ""
    if brief.startswith(("{", "[")) or re.search(
        r'"(?:status|result|answer|code)"\s*:', brief
    ):
        return ""
    return brief


def _fallback_realtime_brief(
    *,
    display_result: str,
    result_kind: str,
) -> tuple[str, str]:
    derived = _safe_brief(display_result)
    if derived and len(derived) <= 100:
        return derived, "derived"
    messages = {
        "research": "资料已核实，完整结论和必要来源已经显示在界面中。",
        "code": "代码已经生成，完整内容已经显示在界面中。",
        "calculation": "计算已经完成，完整结果和过程已经显示在界面中。",
        "file": "文件任务已经完成，完整结果已经显示在界面中。",
        "action": "任务已经执行完成，详细结果已经显示在界面中。",
        "generic": "任务已经完成，完整结果已经显示在界面中。",
    }
    message = messages.get(result_kind)
    if message is None:
        message = messages.get("generic", "")
    return message, "fallback"


def present_core_agent_result(
    raw_answer: str,
    *,
    nonce: str,
    question: str,
    tools_used: list[str] | None = None,
) -> dict[str, Any]:
    """Split Core Agent output into authoritative UI content and a safe spoken brief."""
    begin, end = _brief_markers(nonce)
    pattern = re.compile(
        rf"(?:\r?\n)*{re.escape(begin)}\s*(.*?)\s*{re.escape(end)}(?:\r?\n)*",
        re.DOTALL,
    )
    match = pattern.search(raw_answer)
    display_result = pattern.sub("\n", raw_answer, count=1).strip()
    if not display_result:
        display_result = raw_answer.strip()
    kind = _result_kind(question, display_result, tools_used or [])
    model_brief = _safe_brief(match.group(1)) if match else ""
    if model_brief:
        summary, source = model_brief, "core_agent"
    else:
        summary, source = _fallback_realtime_brief(
            display_result=display_result,
            result_kind=kind,
        )
    return {
        "display_result": display_result,
        "realtime_brief": {
            "status": "completed",
            "result_kind": kind,
            "summary": summary,
            "displayed_in_ui": True,
            "response_mode": "brief",
            "source": source,
        },
    }


def _normalized_task(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _delegation_context_text(items: list[dict[str, Any]]) -> str:
    if not items:
        return "无"
    blocks: list[str] = []
    remaining = MAX_DELEGATION_CONTEXT_CHARS
    for index, item in enumerate(items[-MAX_DELEGATION_CONTEXT_ITEMS:], start=1):
        block = (
            f"[{index}] 用户指令：{item.get('question') or item.get('query') or '无'}\n"
            f"执行目标：{item.get('query') or item.get('question') or '无'}\n"
            f"Core Agent 结果：{item.get('result') or '无'}"
        )
        if len(block) > remaining:
            block = block[:remaining].rstrip()
        if not block:
            break
        blocks.append(block)
        remaining -= len(block)
        if remaining <= 0:
            break
    return "\n\n".join(blocks) or "无"


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
    if event_type in {"chat.file", "chat.final"}:
        files = normalize_file_items(payload.get("files"))
        if files:
            return {
                "stage": "file",
                "title": "文件已返回",
                "status": "completed",
                "files": files,
            }
    if event_type == "chat.reasoning":
        content = str(payload.get("content") or "")
        if not content:
            return None
        return {
            "stage": "reasoning",
            "title": "正在分析问题",
            "status": "running",
            "content": content,
        }
    if event_type == "chat.tool_call":
        tool = (
            payload.get("tool_call")
            if isinstance(payload.get("tool_call"), dict)
            else payload
        )
        name = str(
            tool.get("display_name")
            or tool.get("name")
            or payload.get("tool_name")
            or "工具"
        ).strip()
        detail = core_agent_text(tool.get("formatted_args") or tool.get("arguments"))
        return {
            "stage": "tool_call",
            "title": f"调用工具：{name}",
            "detail": detail,
            "status": "running",
            "tool_call_id": str(
                tool.get("id")
                or tool.get("tool_call_id")
                or payload.get("tool_call_id")
                or ""
            ),
            "tool_name": str(
                tool.get("name") or payload.get("tool_name") or "unknown"
            ).strip(),
            "tool_arguments": tool.get("arguments")
            if tool.get("arguments") is not None
            else {},
            "tool_description": str(tool.get("description") or "").strip(),
            "tool_formatted_args": str(tool.get("formatted_args") or "").strip(),
            "tool_display_name": str(tool.get("display_name") or "").strip(),
        }
    if event_type == "chat.tool_update":
        update = (
            payload.get("tool_update")
            if isinstance(payload.get("tool_update"), dict)
            else payload
        )
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
        result = (
            payload.get("tool_result")
            if isinstance(payload.get("tool_result"), dict)
            else payload
        )
        name = str(result.get("tool_name") or result.get("name") or "工具").strip()
        raw_status = str(result.get("status") or "").strip().lower()
        failed = result.get("success") is False or raw_status in {
            "error",
            "failed",
            "failure",
            "timeout",
            "timed_out",
        }
        detail = core_agent_text(
            result.get("summary") or result.get("error") or result.get("result")
        )
        raw_result = result.get("result")
        if raw_result is None:
            raw_result = result.get("raw_output")
        if raw_result is None:
            raw_result = result.get("data")
        if raw_result is None:
            raw_result = result.get("error") or result.get("summary")
        return {
            "stage": "tool_result",
            "title": f"{name}{'执行失败' if failed else '执行完成'}",
            "detail": detail,
            "status": "failed" if failed else "completed",
            "tool_call_id": str(
                result.get("tool_call_id") or payload.get("tool_call_id") or ""
            ),
            "tool_name": name,
            "tool_result": raw_result,
            "tool_summary": str(result.get("summary") or "").strip(),
            "tool_success": not failed,
        }
    if event_type == "todo.updated":
        todos = payload.get("todos")
        if not isinstance(todos, list) or not todos:
            return None
        completed = 0
        for item in todos:
            if not isinstance(item, dict):
                continue
            status = str(item.get("status") or "").lower()
            if status == "completed":
                completed += 1
        return {
            "stage": "plan",
            "title": "执行计划已更新",
            "detail": f"{completed}/{len(todos)} 项已完成",
            "status": "running",
            "todos": [
                {
                    "id": str(item.get("id") or index),
                    "content": str(item.get("content") or item.get("activeForm") or ""),
                    "status": str(item.get("status") or "pending"),
                }
                for index, item in enumerate(todos)
                if isinstance(item, dict)
            ],
        }
    if event_type == "chat.delta" and str(payload.get("content") or "").strip():
        return {"stage": "answer", "title": "正在整理执行结果", "status": "running"}
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
    core_session_id: str = "",
    delegation_context: list[dict[str, Any]] | None = None,
    frame_data_url: str = "",
    normalize_media_attachments: Callable[[dict[str, Any], str | None], None]
    | None = None,
    on_progress: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """Run one delegated video task through the standard, full Core Agent API."""
    from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
    from jiuwenswarm.common.schema.message import ReqMethod

    client = (
        agent_client.get("value") if isinstance(agent_client, dict) else agent_client
    )
    if client is None:
        raise RuntimeError("AgentServer client is unavailable")
    request_id = f"video-core-{uuid.uuid4().hex}"
    brief_nonce = uuid.uuid4().hex
    core_session_id = core_session_id or f"video-tool-{uuid.uuid4().hex}"
    context_text = _delegation_context_text(delegation_context or [])
    prompt = (
        f"用户原始指令：{question or query}\n"
        f"Realtime 模型整理的执行目标：{query or question}\n"
        f"Realtime视觉模型提供的画面线索：{visual_context or '无'}\n\n"
        f"同一 Full-duplex 会话此前已完成的委托：\n{context_text}\n\n"
        "请完整执行用户原始指令。原始指令是权威需求，Realtime 模型整理的目标只用于补充上下文，"
        "不得覆盖、缩减或改变原始指令中的动作、对象、路径、输出格式及限制。"
        "你可以使用当前 Core Agent 可用的全部工具和能力，不要把任务限制为联网搜索。"
        "请求附带当前视频帧时，如果任务涉及画面中的实体、文字或指代，可先使用图片理解工具核对画面。"
        "优先复用此前委托中已经定位的文件、网址、数据和执行结果；已有信息足以完成任务时，不要重新扫描文件系统、"
        "重复抓取网页或再次识别无关的视频帧。"
        "需要外部或时效性事实时，必须使用搜索及网页正文核实。\n\n"
        "最终回答要求：必须使用简体中文。完成必要操作后直接回应用户原始指令，只保留执行结果、必要依据和必要来源，"
        "不得复述工具调用、抓取、重试或核实过程。回答的格式与详略服从用户原始指令；用户未指定时保持简洁。"
        f"{core_agent_brief_protocol(brief_nonce)}"
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
        "video_core_session_id": core_session_id,
        "video_delegation_context": delegation_context or [],
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
        file_progress = core_agent_progress({**payload, "event_type": "chat.final"})
        if file_progress and on_progress is not None:
            await on_progress(file_progress)
        raw_answer = str(payload.get("content") or payload.get("answer") or "").strip()
        if not raw_answer and file_progress:
            raw_answer = "文件已返回，请查看产物。"
        if not raw_answer:
            raise RuntimeError("Jiuwen Core Agent returned empty output")
        presented = present_core_agent_result(
            raw_answer,
            nonce=brief_nonce,
            question=question or query,
            tools_used=[],
        )
        return {
            **payload,
            **presented,
            "answer": presented["display_result"],
            "raw_answer_chars": len(raw_answer),
            "tools_used": [],
        }

    final_payload: dict[str, Any] = {}
    delta_parts: list[str] = []
    tools_used: list[str] = []
    emitted_once: set[str] = set()
    received_files = False
    async for chunk in send_stream(env):
        payload = chunk.payload if isinstance(chunk.payload, dict) else {}
        event_type = str(payload.get("event_type") or "").strip()
        if event_type == "chat.error":
            raise RuntimeError(
                str(
                    payload.get("error")
                    or payload.get("content")
                    or "Jiuwen Core Agent failed"
                )
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
        received_files = received_files or stage == "file"
        tool_key = str(progress.get("tool_call_id") or "")
        dedupe_key = f"{stage}:{tool_key}" if tool_key else stage
        # Reasoning is streamed as deltas. Suppressing repeated stages here used to
        # discard every delta after the first one, so the task timeline could never
        # reproduce the Core Agent's actual reasoning path.
        if stage in {"answer", "plan"} and dedupe_key in emitted_once:
            continue
        emitted_once.add(dedupe_key)
        tool_name = str(progress.get("tool_name") or "").strip()
        if tool_name and tool_name not in tools_used:
            tools_used.append(tool_name)
        if on_progress is not None:
            await on_progress(progress)

    raw_answer = (
        str(final_payload.get("content") or "").strip() or "".join(delta_parts).strip()
    )
    if not raw_answer and received_files:
        raw_answer = "文件已返回，请查看产物。"
    if not raw_answer:
        raise RuntimeError("Jiuwen Core Agent returned empty output")
    presented = present_core_agent_result(
        raw_answer,
        nonce=brief_nonce,
        question=question or query,
        tools_used=tools_used,
    )
    return {
        **final_payload,
        **presented,
        "answer": presented["display_result"],
        "raw_answer_chars": len(raw_answer),
        "tools_used": tools_used,
    }


class VideoSearchManager:
    """Own search-job lifecycle and recovery state for one video RPC registry."""

    def __init__(
        self,
        channel: Any,
        agent_client: Any,
        *,
        normalize_media_attachments: Callable[[dict[str, Any], str | None], None]
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
        self._session_states: dict[str, dict[str, Any]] = {}
        self._call_jobs: dict[tuple[str, str], str] = {}
        self._queue: dict[str, list[str]] = {}
        self._active: dict[str, str] = {}
        self._job_tasks: dict[str, asyncio.Task] = {}
        self._changed = asyncio.Condition()
        self._queue_versions: dict[str, int] = {}
        self._controls: dict[str, asyncio.Lock] = {}
        self._stopping: dict[str, asyncio.Event] = {}

    def _queue_snapshot(self, scope: str) -> dict[str, Any]:
        pending = self._queue.get(scope, [])
        jobs: list[dict[str, Any]] = []
        visible_statuses = {"queued", "running", "cancelling", "cancelled"}
        public_keys = (
            "job_id",
            "search_session_id",
            "question",
            "query",
            "status",
            "tool_call_id",
            "turn_id",
        )
        for job_id, job in self._jobs.items():
            if job.get("search_session_id") != scope:
                continue
            if job.get("status") not in visible_statuses:
                continue
            public_job: dict[str, Any] = {}
            for key in public_keys:
                if key in job:
                    public_job[key] = job.get(key)
            public_job["queue_position"] = (
                pending.index(job_id) + 1 if job_id in pending else 0
            )
            public_job["queue_version"] = self._queue_versions.get(scope, 0)
            jobs.append(public_job)
        return {
            "search_session_id": scope,
            "queue_version": self._queue_versions.get(scope, 0),
            "jobs": jobs,
        }

    async def _publish_queue(self, ws: Any, scope: str) -> None:
        self._queue_versions[scope] = self._queue_versions.get(scope, 0) + 1
        await self._send_event(ws, "video.search.queue", self._queue_snapshot(scope))

    @asynccontextmanager
    async def _job_slot(self, ws: Any, scope: str, job_id: str):
        async with self._changed:
            await self._changed.wait_for(
                lambda: (
                    scope not in self._active
                    and self._queue.get(scope, [])[:1] == [job_id]
                    and job_id not in self._stopping
                )
            )
            self._queue[scope].remove(job_id)
            self._active[scope] = job_id
        try:
            await self._publish_queue(ws, scope)
            yield
        finally:
            stopping = self._stopping.get(job_id)
            if stopping is not None:
                await stopping.wait()
            if self._jobs[job_id].get("status") == "running":
                self._jobs[job_id]["status"] = "failed"
            async with self._changed:
                self._active.pop(scope, None)
                self._changed.notify_all()

    async def _wait_for_stop(self, job_id: str) -> None:
        stopping = self._stopping.get(job_id)
        if stopping is not None:
            await stopping.wait()

    async def _cancel_job(self, ws: Any, scope: str, job_id: str) -> None:
        job = self._jobs[job_id]
        if job.get("status") in {"completed", "failed", "cancelled"}:
            return
        old_status = job["status"]
        stopped = asyncio.Event()
        self._stopping[job_id] = stopped
        job["status"] = "cancelling"
        await self._publish_queue(ws, scope)
        try:
            if old_status == "running":
                from jiuwenswarm.common.e2a.gateway_normalize import (
                    e2a_from_agent_fields,
                )
                from jiuwenswarm.common.schema.message import ReqMethod

                client = (
                    self._agent_client.get("value")
                    if isinstance(self._agent_client, dict)
                    else self._agent_client
                )
                core_session_id = self._session_state(scope)["core_session_id"]
                env = e2a_from_agent_fields(
                    request_id=f"video-cancel-{uuid.uuid4().hex}",
                    channel_id=VIDEO_TOOL_CHANNEL_ID,
                    session_id=core_session_id,
                    req_method=ReqMethod.CHAT_CANCEL,
                    params={
                        "intent": "cancel",
                        "mode": "agent",
                        "work_mode": "work",
                        "session_id": core_session_id,
                    },
                    is_stream=False,
                    timestamp=time.time(),
                )
                response = await asyncio.wait_for(client.send_request(env), timeout=20)
                payload = response.payload if isinstance(response.payload, dict) else {}
                if not response.ok or payload.get("success") is False:
                    raise RuntimeError(
                        str(payload.get("error") or "Core Agent 未确认停止")
                    )
        except BaseException:
            self._jobs[job_id]["status"] = old_status
            stopped.set()
            self._stopping.pop(job_id, None)
            await self._publish_queue(ws, scope)
            raise
        stopped.set()
        task = self._job_tasks.get(job_id)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._stopping.pop(job_id, None)
        async with self._changed:
            if job_id in self._queue.get(scope, []):
                self._queue[scope].remove(job_id)
            self._changed.notify_all()
        current = self._jobs[job_id]
        history = list(current.get("progress_history") or [])
        history.append(
            {
                "stage": "cancelled",
                "status": "cancelled",
                "title": "用户已停止任务",
                "sequence": len(history) + 1,
                "timestamp": time.time(),
            }
        )
        current.update(status="cancelled", progress_history=history)
        await self._send_event(ws, "video.search.cancelled", current)
        await self._publish_queue(ws, scope)
        await asyncio.to_thread(
            self._log_event, {"stage": "search_cancelled", **current}
        )

    async def handle_control(
        self, ws: Any, req_id: Any, params: Any, session_id: Any
    ) -> None:
        raw = params if isinstance(params, dict) else {}
        scope = str(raw.get("search_session_id") or "")
        try:
            job_id = str(raw.get("job_id") or "")
            job = self._jobs.get(job_id)
            if not scope or not job or job.get("search_session_id") != scope:
                raise ValueError("任务不存在或不属于当前队列")
            async with self._controls.setdefault(scope, asyncio.Lock()):
                action = raw.get("action")
                if action == "cancel":
                    await self._cancel_job(ws, scope, job_id)
                elif action in {"next", "before", "preempt"}:
                    pending = self._queue.get(scope, [])
                    previous_order = list(pending)
                    if raw.get("queue_version") != self._queue_versions.get(scope, 0):
                        raise ValueError("任务队列已变化，请按最新顺序重试")
                    if job_id not in pending or job.get("status") != "queued":
                        raise ValueError("只能调整尚未开始的任务")
                    target = str(raw.get("before_job_id") or "")
                    if action == "before" and target and target not in pending:
                        raise ValueError("目标任务已开始，请刷新队列")
                    if target != job_id:
                        pending.remove(job_id)
                        index = (
                            pending.index(target)
                            if action == "before" and target
                            else (len(pending) if action == "before" else 0)
                        )
                        pending.insert(index, job_id)
                    if action == "preempt" and scope in self._active:
                        try:
                            await self._cancel_job(ws, scope, self._active[scope])
                        except Exception:
                            pending[:] = [
                                item for item in previous_order if item in pending
                            ] + [item for item in pending if item not in previous_order]
                            raise
                    async with self._changed:
                        self._changed.notify_all()
                    await self._publish_queue(ws, scope)
                    await asyncio.to_thread(
                        self._log_event,
                        {
                            "stage": "search_queue_reordered",
                            "search_session_id": scope,
                            "job_id": job_id,
                            "action": action,
                            "order": list(pending),
                        },
                    )
                else:
                    raise ValueError("不支持的任务操作")
            await self._channel.send_response(
                ws, req_id, ok=True, payload=self._queue_snapshot(scope)
            )
        except Exception as exc:
            if scope in self._queue:
                await self._publish_queue(ws, scope)
            await self._channel.send_response(
                ws,
                req_id,
                ok=False,
                error=str(exc) or "未能确认停止，请重试",
                code="QUEUE_CONTROL_FAILED",
            )

    def _session_state(self, search_session_id: str) -> dict[str, Any]:
        state = self._session_states.get(search_session_id)
        if state is None:
            if len(self._session_states) >= self._max_cached_jobs:
                active_sessions = {
                    str(job.get("search_session_id") or "")
                    for job in self._jobs.values()
                    if job.get("status") in {"queued", "running", "cancelling"}
                }
                for candidate in list(self._session_states):
                    if candidate not in active_sessions:
                        self._session_states.pop(candidate, None)
                        self._queue.pop(candidate, None)
                        self._queue_versions.pop(candidate, None)
                        self._controls.pop(candidate, None)
                        break
            state = {
                "core_session_id": f"video-tool-{uuid.uuid4().hex}",
                "delegation_context": [],
            }
            self._session_states[search_session_id] = state
        return state

    @staticmethod
    def _public_job(job: dict[str, Any], *, reused: bool = False) -> dict[str, Any]:
        return {
            "id": str(job.get("job_id") or job.get("id") or ""),
            "status": str(job.get("status") or "running"),
            "question": str(job.get("question") or ""),
            "query": str(job.get("query") or ""),
            "search_session_id": str(job.get("search_session_id") or ""),
            **(
                {"tool_call_id": str(job.get("tool_call_id"))}
                if job.get("tool_call_id")
                else {}
            ),
            **(
                {"tool_name": str(job.get("tool_name"))} if job.get("tool_name") else {}
            ),
            **({"turn_id": str(job.get("turn_id"))} if job.get("turn_id") else {}),
            **({"reused": True} if reused else {}),
        }

    async def _send_event(self, ws: Any, event: str, payload: dict[str, Any]) -> bool:
        try:
            await self._channel.send_event(ws, event, payload)
            return True
        except Exception:  # noqa: BLE001 - progress delivery is best-effort
            logger.debug("Failed to send video search event %s", event, exc_info=True)
            if event in {"video.search.completed", "video.search.failed"}:
                await asyncio.to_thread(
                    self._log_event,
                    {
                        "stage": "search_result_delivery_failed",
                        "event": event,
                        "job_id": str(payload.get("job_id") or ""),
                        "search_session_id": str(
                            payload.get("search_session_id") or ""
                        ),
                    },
                )
            return False

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
        turn_id: str = "",
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
            **({"turn_id": turn_id} if turn_id else {}),
        }

        async def emit_progress(progress: dict[str, Any]) -> None:
            if job_id in self._stopping:
                return
            entry = {
                **progress,
                "sequence": len(progress_history) + 1,
                "elapsed_ms": round((time.perf_counter() - started_at) * 1000),
                "timestamp": time.time(),
            }
            progress_history.append(entry)
            current = self._jobs.get(job_id, {})
            self._jobs[job_id] = {
                **current,
                **base_payload,
                "status": "running",
                "progress_history": list(progress_history),
            }
            await self._send_event(
                ws,
                "video.search.progress",
                {
                    **base_payload,
                    "status": "running",
                    "progress": entry,
                },
            )

        start_progress = {
            "stage": "queued",
            "title": "等待 Core Agent 执行",
            "status": "queued",
            "sequence": 1,
            "elapsed_ms": 0,
            "timestamp": time.time(),
        }
        progress_history.append(start_progress)
        self._jobs[job_id] = {
            **base_payload,
            "status": "cancelling" if job_id in self._stopping else "queued",
            "progress_history": list(progress_history),
        }
        await self._send_event(
            ws,
            "video.search.started",
            {
                **base_payload,
                "status": "queued",
                "progress_history": list(progress_history),
            },
        )
        await self._publish_queue(ws, search_session_id)
        await asyncio.to_thread(
            self._log_event, {"stage": "search_started", **base_payload}
        )
        try:
            session_state = self._session_state(search_session_id)
            core_session_id = str(session_state["core_session_id"])
            async with self._job_slot(ws, search_session_id, job_id):
                async with self._semaphore:
                    await self._wait_for_stop(job_id)
                    await emit_progress(
                        {
                            "stage": "started",
                            "title": "Core Agent 已开始处理",
                            "status": "running",
                        }
                    )
                    delegation_context = list(session_state["delegation_context"])
                    await self._wait_for_stop(job_id)
                    core_result = await execute_core_agent(
                        self._agent_client,
                        question=question,
                        query=query,
                        visual_context=visual_context,
                        search_session_id=search_session_id,
                        core_session_id=core_session_id,
                        delegation_context=delegation_context,
                        frame_data_url=frame_data_url,
                        normalize_media_attachments=self._normalize_media_attachments,
                        on_progress=emit_progress,
                    )
                    await self._wait_for_stop(job_id)
                    answer = core_result["answer"]
                    realtime_brief = core_result["realtime_brief"]
                    session_state["delegation_context"].append(
                        {
                            "question": question,
                            "query": query,
                            "result": answer[:MAX_DELEGATION_RESULT_CHARS],
                        }
                    )
                    del session_state["delegation_context"][
                        :-MAX_DELEGATION_CONTEXT_ITEMS
                    ]
                    await asyncio.to_thread(
                        self._log_event,
                        {
                            "stage": "core_agent_completed",
                            **base_payload,
                            "core_session_id": core_session_id,
                            "delegation_context_items": len(delegation_context),
                            "tools_used": core_result.get("tools_used", []),
                            "model": core_result.get("model", ""),
                            "answer_chars": len(answer),
                        },
                    )
                    await self._wait_for_stop(job_id)
                    self._jobs[job_id]["status"] = "completed"
            if not answer:
                raise RuntimeError("Jiuwen Core Agent returned empty output")
            latency_ms = round((time.perf_counter() - started_at) * 1000)
            progress_history.append(
                {
                    "stage": "completed",
                    "title": "Core Agent 已完成任务",
                    "status": "completed",
                    "sequence": len(progress_history) + 1,
                    "elapsed_ms": latency_ms,
                    "timestamp": time.time(),
                }
            )
            completed_payload = {
                **base_payload,
                "status": "completed",
                "result": answer,
                "display_result": answer,
                "realtime_brief": realtime_brief,
                "latency_ms": latency_ms,
                "progress_history": list(progress_history),
            }
            self._jobs[job_id] = completed_payload
            await asyncio.to_thread(
                self._log_event, {"stage": "search_completed", **completed_payload}
            )
            delivered = await self._send_event(
                ws, "video.search.completed", completed_payload
            )
            if not delivered:
                self._jobs[job_id] = {**completed_payload, "delivery_pending": True}
        except Exception as exc:  # noqa: BLE001
            await self._wait_for_stop(job_id)
            error = str(exc).strip() or "Jiuwen Core Agent failed"
            latency_ms = round((time.perf_counter() - started_at) * 1000)
            progress_history.append(
                {
                    "stage": "failed",
                    "title": "Core Agent 执行失败",
                    "detail": core_agent_text(error),
                    "status": "failed",
                    "sequence": len(progress_history) + 1,
                    "elapsed_ms": latency_ms,
                    "timestamp": time.time(),
                }
            )
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
            delivered = await self._send_event(
                ws, "video.search.failed", failed_payload
            )
            if not delivered:
                self._jobs[job_id] = {**failed_payload, "delivery_pending": True}

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
        turn_id: str = "",
    ) -> dict[str, Any]:
        job_id = uuid.uuid4().hex
        search_job = {
            "id": job_id,
            "status": "queued",
            "question": question,
            "query": query,
            "search_session_id": search_session_id,
            **({"tool_call_id": tool_call_id} if tool_call_id else {}),
            **({"tool_name": tool_name} if tool_name else {}),
            **({"turn_id": turn_id} if turn_id else {}),
        }
        if len(self._jobs) >= self._max_cached_jobs:
            # An active queued job must remain recoverable through the status API.
            oldest_terminal = next(
                (
                    key
                    for key, job in self._jobs.items()
                    if job.get("status") in {"completed", "failed", "cancelled"}
                ),
                None,
            )
            if oldest_terminal:
                self._jobs.pop(oldest_terminal)
        self._jobs[job_id] = {"job_id": job_id, **search_job}
        self._queue.setdefault(search_session_id, []).append(job_id)
        task = asyncio.create_task(
            self._run_job(
                ws,
                job_id=job_id,
                search_session_id=search_session_id,
                question=question,
                query=query,
                visual_context=visual_context,
                frame_data_url=frame_data_url,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                turn_id=turn_id,
            )
        )
        self._tasks.add(task)
        self._job_tasks[job_id] = task
        task.add_done_callback(lambda _task: self._job_tasks.pop(job_id, None))
        task.add_done_callback(self._tasks.discard)
        if tool_call_id:
            self._call_jobs[(search_session_id, tool_call_id)] = job_id
        return search_job

    def find_running(
        self, *, query: str, search_session_id: str
    ) -> dict[str, Any] | None:
        normalized_query = _normalized_task(query)
        if not normalized_query:
            return None
        for job in reversed(list(self._jobs.values())):
            if (
                job.get("status") in {"queued", "running"}
                and job.get("search_session_id") == search_session_id
                and _normalized_task(str(job.get("query") or "")) == normalized_query
            ):
                return self._public_job(job, reused=True)
        return None

    def _find_idempotent_job(
        self,
        *,
        search_session_id: str,
        call_id: str,
    ) -> dict[str, Any] | None:
        job_id = self._call_jobs.get((search_session_id, call_id)) if call_id else None
        job = self._jobs.get(job_id or "")
        return self._public_job(job, reused=True) if job else None

    async def _replay_terminal_job(self, ws: Any, search_job: dict[str, Any]) -> None:
        job = self._jobs.get(str(search_job.get("id") or ""))
        if not job:
            return
        status = str(job.get("status") or "")
        if status == "completed":
            await self._send_event(ws, "video.search.completed", job)
        elif status == "failed":
            await self._send_event(ws, "video.search.failed", job)
        elif status == "cancelled":
            await self._send_event(ws, "video.search.cancelled", job)

    async def handle_qwen_tool(
        self, ws: Any, req_id: Any, params: Any, session_id: Any
    ) -> None:
        del session_id
        raw_params = params if isinstance(params, dict) else {}
        question = str(raw_params.get("question") or "").strip()
        search_session_id = str(raw_params.get("search_session_id") or "").strip()
        frame_data_url = str(raw_params.get("frame_data_url") or "").strip()
        turn_id = str(raw_params.get("turn_id") or "").strip()
        request_log = {
            "stage": "qwen_tool_requested",
            "request_id": str(req_id),
            "name": str(raw_params.get("name") or "").strip(),
            "call_id": str(raw_params.get("call_id") or "").strip(),
            "question": question,
            "search_session_id": search_session_id,
            "turn_id": turn_id,
        }
        await asyncio.to_thread(self._log_event, request_log)
        try:
            if not self._qwen_active():
                raise ValueError("Qwen Omni Realtime is not the active video provider")
            tool_call = parse_qwen_omni_tool_call(raw_params)
            if not question:
                question = tool_call.task
            if len(question) > MAX_ORIGINAL_INSTRUCTION_CHARS:
                raise ValueError(
                    "question must not exceed "
                    f"{MAX_ORIGINAL_INSTRUCTION_CHARS} characters"
                )
            if not search_session_id or len(search_session_id) > 200:
                raise ValueError("search_session_id must contain 1-200 characters")
            if len(turn_id) > MAX_TURN_ID_CHARS:
                raise ValueError(
                    f"turn_id must not exceed {MAX_TURN_ID_CHARS} characters"
                )
            if frame_data_url and (
                len(frame_data_url) > MAX_FRAME_CHARS
                or _frame_media_item(frame_data_url) is None
            ):
                raise ValueError("frame_data_url is invalid")
        except ValueError as exc:
            error = str(exc)
            await asyncio.to_thread(
                self._log_event,
                {
                    **request_log,
                    "stage": "qwen_tool_rejected",
                    "error": error,
                },
            )
            await self._channel.send_response(
                ws, req_id, ok=False, error=error, code="BAD_REQUEST"
            )
            return

        # A repeated transport request keeps the same call_id and can safely reuse the
        # original job. Different Qwen call IDs still require distinct tool outputs,
        # so they must not be collapsed here even when their text looks similar.
        search_job = self._find_idempotent_job(
            search_session_id=search_session_id,
            call_id=tool_call.call_id,
        )
        if search_job is None:
            search_job = self.start(
                ws,
                question=question,
                query=tool_call.task,
                search_session_id=search_session_id,
                frame_data_url=frame_data_url,
                tool_call_id=tool_call.call_id,
                tool_name=tool_call.name,
                turn_id=turn_id,
            )
        else:
            await asyncio.to_thread(
                self._log_event,
                {
                    **request_log,
                    "stage": "qwen_tool_reused",
                    "task": tool_call.task,
                    "job_id": search_job["id"],
                    "job_status": search_job["status"],
                },
            )
        await asyncio.to_thread(
            self._log_event,
            {
                **request_log,
                "stage": "qwen_tool_accepted",
                "task": tool_call.task,
                "job_id": search_job["id"],
            },
        )
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
        if search_job.get("reused") and search_job.get("status") in {
            "completed",
            "failed",
            "cancelled",
        }:
            await self._replay_terminal_job(ws, search_job)

    async def handle_status(
        self, ws: Any, req_id: Any, params: Any, session_id: Any
    ) -> None:
        del session_id
        job_id = (
            str(params.get("job_id") or "").strip() if isinstance(params, dict) else ""
        )
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
        scope = str(job.get("search_session_id") or "")
        pending = self._queue.get(scope, [])
        await self._channel.send_response(
            ws,
            req_id,
            ok=True,
            payload={
                **job,
                "queue_position": pending.index(job_id) + 1 if job_id in pending else 0,
                "queue_version": self._queue_versions.get(scope, 0),
            },
        )
        if job.get("delivery_pending"):
            job["delivery_pending"] = False
            await asyncio.to_thread(
                self._log_event,
                {
                    "stage": "search_result_recovered_by_status",
                    "job_id": job_id,
                    "search_session_id": str(job.get("search_session_id") or ""),
                    "status": str(job.get("status") or ""),
                },
            )
