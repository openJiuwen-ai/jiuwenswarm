"""Core Agent execution and asynchronous search jobs for video-live sessions."""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Awaitable, Callable
import uuid

from jiuwenswarm.extensions.video_duplex.backend.video_files import normalize_file_items

logger = logging.getLogger(__name__)
VIDEO_TOOL_CHANNEL_ID = "video_tool"


_IMAGE_FILENAME_SUFFIXES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
MAX_FRAME_CHARS = 4_000_000
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
    request_id: str = "",
    user_id: str | None = None,
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
    request_id = request_id or f"video-core-{uuid.uuid4().hex}"
    brief_nonce = uuid.uuid4().hex
    core_session_id = core_session_id or f"video-tool-{uuid.uuid4().hex}"
    context_text = _delegation_context_text(delegation_context or [])
    prompt = (
        f"用户任务要求（包括按时间顺序追加的用户修改）：{question or query}\n"
        f"Realtime 模型整理的执行目标：{query or question}\n"
        f"Realtime视觉模型提供的画面线索：{visual_context or '无'}\n\n"
        f"同一 Full-duplex 会话此前已完成的委托：\n{context_text}\n\n"
        "请完整执行用户任务要求。用户明确追加的修改或后续修订优先于与其冲突的早期要求；"
        "未被修改的要求、权限和限制继续有效。Realtime 模型整理的目标和历史结果只用于补充上下文，"
        "不得覆盖最新用户要求中的动作、对象、路径、输出格式及限制。"
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
        user_id=user_id,
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

    if not final_payload:
        from jiuwenswarm.runtime.tasks.service import ExecutionUncertain
        raise ExecutionUncertain("Agent stream ended without a final result; execution is unknown")
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



# Keep the established import path; lifecycle ownership lives in Host TaskService.
from .task_adapter import VideoSearchManager  # noqa: E402,F401
