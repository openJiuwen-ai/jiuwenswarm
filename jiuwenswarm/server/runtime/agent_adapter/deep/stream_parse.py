"""解析 SDK / runner 的流式 chunk，生成对外事件、文本载荷和 HITL 相关判断。"""

from __future__ import annotations
import logging
from typing import Any
from jiuwenswarm.server.runtime.agent_adapter.refusal_text_scrub import is_refusal_scrub_control
from jiuwenswarm.server.runtime.agent_adapter.todo_close_text_hold import is_todo_close_control
from jiuwenswarm.agents.harness.common.tools.deepresearch.deepresearch_rewrite_fast_path import (
    RewriteFastPathResult,
)
from jiuwenswarm.agents.harness.common.tools.deepresearch.deepresearch_rewrite_html_followup import (
    RewriteHtmlFollowupResult,
)
from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
    convert_interactions_to_ask_user_question,
)
from jiuwenswarm.server.runtime.agent_adapter.subagent_stream import try_handle_subagent_chunk
from jiuwenswarm.server.utils.stream_utils import (
    build_tool_result_payload,
    normalize_context_usage_payload,
    parse_ask_user_question_payload,
)
from jiuwenswarm.common.schema.agent import AgentResponseChunk
from jiuwenswarm.server.runtime.agent_adapter.deep.pure_helpers import (
    _extract_usage_metadata,
    _interaction_goal_updated_payload,
)

logger = logging.getLogger(__name__)


def _propagate_stream_source_id(
    src_payload: Any, result: dict[str, Any] | None
) -> dict[str, Any] | None:
    """将上游 payload 中的 stream_source_id / task_id 透传到输出帧（原地写入）。

    skill_turbo 并发节点用 stream_source_id 标识 source，前端据其路由到
    subagent 行。task_id 原样透传，前端可按需把 chat.delta 归入
    taskRuns；是否进正式正文由上层决定，本函数不裁剪 content。
    """
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import STREAM_SOURCE_ID_FIELD
    if result is None or not isinstance(src_payload, dict):
        return result
    source_id = src_payload.get(STREAM_SOURCE_ID_FIELD)
    if source_id:
        result[STREAM_SOURCE_ID_FIELD] = source_id
    task_id = src_payload.get("task_id")
    if isinstance(task_id, str) and task_id.strip():
        result["task_id"] = task_id.strip()
    return result


def _rewrite_skill_turbo_usage_chunk(
    chunk: AgentResponseChunk,
    *,
    session_id: str,
) -> tuple[AgentResponseChunk | None, dict[str, Any] | None]:
    """Map SkillTurbo ``chat.llm_usage`` onto the adapter's usage_metadata event.

    HITL resume yields AgentResponseChunk payloads directly and never passes
    through the DeepAgent ``type='llm_usage'`` consumer. Testers grep
    ``chat.usage_metadata`` / ``chat.usage_summary``, not ``chat.llm_usage``.
    """
    payload = chunk.payload
    usage_meta = _extract_usage_metadata(payload)
    if usage_meta is None:
        return None, None
    if (
        isinstance(payload, dict)
        and payload.get("event_type") == "chat.usage_metadata"
    ):
        return chunk, usage_meta
    return (
        AgentResponseChunk(
            request_id=chunk.request_id,
            channel_id=chunk.channel_id,
            payload={
                "event_type": "chat.usage_metadata",
                "metadata": payload,
                "session_id": session_id,
            },
            is_complete=False,
            agent_ref=chunk.agent_ref,
            metadata=chunk.metadata,
        ),
        usage_meta,
    )


def _iter_delta_for_unstreamed_final(
    *, content: str, chunk_payload: Any
) -> list[dict[str, Any]]:
    """终稿正文未被 chat.delta 真正流式送达时，构造补发 delta 的 payload 列表。

    非 team 前端只按 chat.delta 渲染正文，chat.final 仅作收尾标记（有的前端
    会直接丢弃其 content）。当模型把正文只放进 answer chunk、而 llm_output
    只给出空白/极小片段时，这里补发 delta，保证前端能看到回复。
    """
    text = str(content or "")
    if not text.strip():
        return []
    payload = _stream_text_payload("chat.delta", text)
    if payload is None:
        return []
    _propagate_stream_source_id(chunk_payload, payload)
    logger.info(
        "[JiuWenSwarmDeepAdapter] rerouted final content into chat.delta: len=%s",
        len(text),
    )
    return [payload]


def _drain_final_content_to_deltas(
    parsed: dict[str, Any],
    *,
    segment_streamed_text: str,
    chunk_payload: Any,
    preserve_final_content: bool = False,
) -> list[dict[str, Any]]:
    """Move non-empty ``chat.final`` body onto ``chat.delta`` when needed.

    By default clears ``parsed["content"]`` once the body is known to be (or
    about to be) on the delta path. Leaving content on ``chat.final`` makes
    the client append another copy when it derives text from the final frame,
    especially after the post-tool ``\\n\\n`` round-break prefix, triplicating
    the bubble.

    ``preserve_final_content`` (cron_* web follow-up): still emit rescue
    deltas, but keep final body so Gateway history / live-bus mirror can
    fall back when delta frames are dropped. Web UI treats identical final
    as overwrite, not append.
    """
    if parsed.get("event_type") != "chat.final":
        return []
    content = str(parsed.get("content") or "")
    stripped = content.strip()
    if not stripped:
        return []
    streamed = str(segment_streamed_text or "")
    if stripped in streamed or streamed.rstrip().endswith(stripped):
        if not preserve_final_content:
            parsed["content"] = ""
        return []
    streamed_visible = streamed.strip()
    # llm_output already delivered a substantial body this segment; keeping
    # final content would re-route a duplicate chat.delta.
    if streamed_visible and len(streamed_visible) >= max(8, len(stripped) // 4):
        if not preserve_final_content:
            parsed["content"] = ""
        return []
    deltas = _iter_delta_for_unstreamed_final(
        content=content,
        chunk_payload=chunk_payload,
    )
    # Body rides on delta; final is trailer-only for non-team hosts —
    # unless cron_* needs final as history/live fallback.
    if not preserve_final_content:
        parsed["content"] = ""
    return deltas


def _approval_chunk_from_event(event: Any) -> dict[str, Any] | None:
    parsed = _parse_stream_chunk(event)
    if not isinstance(parsed, dict) or parsed.get("event_type") != "chat.ask_user_question":
        return None
    request_id = parsed.get("request_id")
    questions = parsed.get("questions")
    if not isinstance(request_id, str) or not request_id.strip():
        return None
    if not isinstance(questions, list) or not questions:
        return None
    return parsed


def _fast_path_chunks(
    result: RewriteFastPathResult,
    *,
    request_id: str,
    channel_id: str,
) -> tuple[AgentResponseChunk, ...]:
    if result.status == "completed":
        payload = {
            "event_type": "chat.final",
            "content": result.message,
            "status": "completed",
        }
        if result.error_code:
            payload["error_code"] = result.error_code
    else:
        code = result.error_code or "INTERNAL_ERROR"
        payload = {
            "event_type": "chat.error",
            "error": f"改写失败（{code}）：{result.message}",
            "status": "error",
            "error_code": code,
        }
    return (
        AgentResponseChunk(
            request_id=request_id,
            channel_id=channel_id,
            payload=payload,
            is_complete=False,
        ),
    )


def _rewrite_html_followup_chunks(
    result: RewriteHtmlFollowupResult,
    *,
    request_id: str,
    channel_id: str,
) -> tuple[AgentResponseChunk, ...]:
    return (
        AgentResponseChunk(
            request_id=request_id,
            channel_id=channel_id,
            payload={
                "event_type": "chat.final",
                "content": result.message,
            },
            is_complete=False,
        ),
    )


def _stream_text_payload(
    event_type: str,
    content: Any,
) -> dict[str, Any] | None:
    """Build a text event without discarding formatting-only chunks."""
    if content is None or content == "":
        return None
    return {"event_type": event_type, "content": content}


def _is_ask_user_payload(payload: Any) -> bool:
    """HITL 暂停判定：是否为会打断外层流的 checkpoint ask_user 卡片。

    子 Agent 托管审批（``subagent_tool_permission`` / ``subagent_skill_load``）
    只是在父会话转发一张卡并 await Future，外层 round 仍在跑；不能置
    ``suppress_stream_after_hitl`` / 发 ``chat.invocation_paused``，否则点击
    作答后父流被误收口、任务看起来“直接结束”。
    """
    if not isinstance(payload, dict):
        return False
    if payload.get("event_type") != "chat.ask_user_question":
        return False
    source = str(payload.get("source") or "").strip()
    return source not in {"subagent_skill_load", "subagent_tool_permission"}


def _is_hitl_suppress_noise_chunk(chunk: Any) -> bool:
    """Metadata that may follow ask_user before the stream truly pauses.

    These must not clear suppress / hitl_pending; any other chunk means the
    runner resumed in-place and outbound must resume. Includes non-fatal
    ``controller_output`` tails that arrive with the ask_user card itself
    (not a user-answer resume).

    ``__interaction__`` is NOT noise: after the forced-emit revert it is the
    only source of the ask_user card and must clear suppress to be
    forwarded to the frontend.

    ``controller_output.task_failed`` is NOT noise: it must clear suppress
    and surface ``chat.error`` (otherwise HITL stays paused forever).
    """
    ctype = getattr(chunk, "type", None)
    if ctype in ("llm_usage", "context.usage"):
        return True
    if ctype == "controller_output":
        return _run_failure(chunk) is None
    return False


def _apply_hitl_suppress_clear(
    chunk: Any,
    *,
    suppress_stream_after_hitl: bool,
    request_id: str = "",
) -> tuple[bool, bool]:
    """HITL suppress 清除决策，返回 (suppress_after, skip_chunk)。

    ask_user 之后的 chunk：噪声（llm_usage / context.usage）继续抑制并跳过；
    协议化恢复信号（``__resume_signal__``，agent-core resume 分支发出）精确
    清除 suppress 且自身不转发；其余首个非噪声 chunk 兜底清除 suppress
    （恢复转发，防 invocation 永久挂起——旧版 agent-core 无信号时仍工作）。
    ``hitl_pending_stream`` 不在此处触碰——由调用方持有，驱动
    ``chat.invocation_paused`` 收尾帧，气泡保持开启等待用户输入。
    """
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import _RESUME_SIGNAL_CHUNK_TYPE
    if not suppress_stream_after_hitl:
        return suppress_stream_after_hitl, False
    if getattr(chunk, "type", None) == _RESUME_SIGNAL_CHUNK_TYPE:
        logger.info(
            "[JiuWenSwarmDeepAdapter] HITL suppress cleared on explicit "
            "resume signal: request_id=%s",
            request_id,
        )
        return False, True
    if _is_hitl_suppress_noise_chunk(chunk):
        return suppress_stream_after_hitl, True
    logger.info(
        "[JiuWenSwarmDeepAdapter] HITL suppress cleared on runner resume: "
        "request_id=%s chunk_type=%s",
        request_id,
        getattr(chunk, "type", None) or type(chunk).__name__,
    )
    return False, False


def _run_failure(chunk) -> tuple[str, str] | None:
    """Return ``(error_type, message)`` if *chunk* is a run-level terminal failure.

    These are failures that end the round and are surfaced to the user as a
    ``chat.error`` by :meth:`_parse_stream_chunk` — a
    ``controller_output``/``task_failed`` (e.g. model call failed) or an
    ``answer`` whose ``result_type`` is ``"error"``. Recoverable
    ``tool_result`` errors are intentionally excluded: the agent loop may
    still retry them, so they must not flip the run status.

    Used only to give the /debug ``run end`` ``status`` triage value; the
    chunk still flows through ``_parse_stream_chunk`` unchanged.
    """
    ctype = getattr(chunk, "type", None)
    payload = getattr(chunk, "payload", None)

    def _get(key, default=None):
        if isinstance(payload, dict):
            return payload.get(key, default)
        return getattr(payload, key, default)

    if ctype == "controller_output" and payload is not None:
        inner_t = getattr(payload, "type", None)
        if inner_t is None and isinstance(payload, dict):
            inner_t = payload.get("type")
        inner_val = getattr(inner_t, "value", inner_t) if inner_t is not None else None
        if inner_val == "task_failed":
            data = getattr(payload, "data", None)
            if data is None and isinstance(payload, dict):
                data = payload.get("data")
            data = data or []
            msg = next(
                (
                    getattr(item, "text", None)
                    for item in data
                    if hasattr(item, "text")
                ),
                None,
            )
            if msg is None and isinstance(data, list):
                msg = next(
                    (
                        item.get("text")
                        for item in data
                        if isinstance(item, dict) and item.get("text")
                    ),
                    None,
                )
            return "task_failed", (msg or "task failed")
        return None

    if ctype == "answer" and _get("result_type") == "error":
        out = _get("output")
        if isinstance(out, dict):
            out = out.get("output")
        return "answer_error", (str(out) if out else "task failed")

    if isinstance(chunk, dict) and chunk.get("result_type") == "error":
        return "answer_error", str(chunk.get("output") or "task failed")

    return None


def _parse_stream_chunk(
    chunk,
    *,
    _has_streamed_content: bool = False,
    _stage: str = "",
    _streamed_text: str = "",
    _protocol_buffer: Any = None,
    _parent_session_id: str | None = None,
) -> dict | None:
    """将 SDK OutputSchema 转为前端可消费的 payload dict.

    Args:
        chunk: OutputSchema 或 dict
        _has_streamed_content: 是否已通过 llm_output 流式发送过内容
        _stage: 当前阶段名称，用于 auto_harness harness.message 事件
        _streamed_text: 本段已通过 chat.delta 发出的可见正文（用于空 final / drain）
        _protocol_buffer: 调用方持有的 StreamProtocolBuffer；跨 chunk 缓冲在 call site
            feed/flush，此处仅吸收关键字以免 TypeError
        _parent_session_id: parent session fallback for native subagent persist

    Returns:
        dict  – 含 event_type 的 payload，或 None（需跳过的帧）。
    """
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
            ERROR_EVENT_TYPE,
            GOAL_UPDATED_EVENT_TYPE,
            STREAM_SOURCE_ID_FIELD,
        )
    _ = _protocol_buffer
    try:
        if hasattr(chunk, "type") and hasattr(chunk, "payload"):
            chunk_type = chunk.type
            payload = chunk.payload

            if is_todo_close_control(chunk_type) or is_refusal_scrub_control(chunk_type):
                return None

            if chunk_type == "steering_consumed":
                # Only the root/leader receipt may divide the user's main
                # conversation. A member's private input is not root input.
                role = getattr(chunk, "role", None)
                role_value = getattr(role, "value", role)
                if role is not None and str(role_value).strip().lower() != "leader":
                    return None
                input_ids = payload.get("input_ids") if isinstance(payload, dict) else None
                if (not isinstance(input_ids, list) or not input_ids
                        or any(not isinstance(value, str) or not value.strip() for value in input_ids)):
                    return None
                return {
                    "event_type": "chat.steering_consumed",
                    "input_ids": list(dict.fromkeys(input_ids)),
                }

            if chunk_type == GOAL_UPDATED_EVENT_TYPE:
                return _interaction_goal_updated_payload(payload)

            if chunk_type == ERROR_EVENT_TYPE:
                err_payload = payload if isinstance(payload, dict) else {}
                return {
                    "event_type": ERROR_EVENT_TYPE,
                    "code": err_payload.get("code", "execution_error"),
                    "message": err_payload.get("message", "execution error"),
                    "goal": err_payload.get("goal"),
                }

            if chunk_type == "controller_output" and payload is not None:
                inner_t = getattr(payload, "type", None)
                if inner_t is None and isinstance(payload, dict):
                    inner_t = payload.get("type")
                inner_val = getattr(inner_t, "value", inner_t) if inner_t is not None else None
                if inner_val == "task_failed":
                    data = getattr(payload, "data", None)
                    if data is None and isinstance(payload, dict):
                        data = payload.get("data")
                    data = data or []
                    error = next(
                        (
                            item.text
                            for item in data
                            if hasattr(item, "text")
                        ),
                        None,
                    )
                    if error is None:
                        error = next(
                            (
                                item.get("text")
                                for item in data
                                if isinstance(item, dict) and item.get("text")
                            ),
                            "任务执行失败",
                        )
                    return {"event_type": "chat.error", "error": error or "任务执行失败"}
                if inner_val == "task_interaction":
                    # native-harness ask_user interrupts
                    # surface here without __interaction__ payloads reaching
                    # the stream. Parse the embedded interrupt result so the
                    # question card still reaches the frontend.
                    from jiuwenswarm.server.utils.stream_utils import (
                        parse_task_interaction_payload,
                    )
                    try:
                        interaction_event = parse_task_interaction_payload(payload)
                    except Exception:
                        logger.exception(
                            "[interface_deep] failed to parse task_interaction payload"
                        )
                        interaction_event = None
                    if interaction_event is not None:
                        return interaction_event
                    logger.warning(
                        "[interface_deep] task_interaction without parsable ask_user payload;"
                        " no question card could be built"
                    )
                # Close the controller_output enum: HITL cards are emitted via
                # ``__interaction__``; remaining types are control-plane metadata.
                # Never fall through to ``str(payload)`` → chat.delta (ISSUE #3892).
                if inner_val not in (
                    "task_completion",
                    "task_interaction",
                    "processing",
                    "all_tasks_processed",
                ):
                    logger.debug(
                        "[interface_deep] drop unhandled controller_output type=%r",
                        inner_val,
                    )
                return None

            if chunk_type == "llm_output":
                # Mirror llm_reasoning: openjiuwen uses "output", SkillTurbo "content".
                content = (
                    (payload.get("content", "") or payload.get("output", ""))
                    if isinstance(payload, dict)
                    else str(payload)
                )
                delta_payload = _stream_text_payload(
                    "chat.delta", content
                )
                return _propagate_stream_source_id(payload, delta_payload)

            if chunk_type == "llm_reasoning":
                content = (
                    (payload.get("content", "") or payload.get("output", ""))
                    if isinstance(payload, dict)
                    else str(payload)
                )
                reasoning_payload = _stream_text_payload(
                    "chat.reasoning", content
                )
                return _propagate_stream_source_id(payload, reasoning_payload)

            if chunk_type == "content_chunk":
                content = (
                    payload.get("content", "") if isinstance(payload, dict) else str(payload)
                )
                delta_payload = _stream_text_payload(
                    "chat.delta", content
                )
                src = payload if isinstance(payload, dict) else {}
                return _propagate_stream_source_id(src, delta_payload)

            if chunk_type == "answer":
                if isinstance(payload, dict):
                    if payload.get("result_type") == "error":
                        err = {
                            "event_type": "chat.error",
                            "error": payload.get("output", "未知错误"),
                        }
                        if payload.get("soft_stop_recovery_exhausted"):
                            err["code"] = "soft_stop_recovery_exhausted"
                        return err
                    output = payload.get("output", {})
                    if isinstance(output, dict) and output.get("result_type") == "error":
                        logger.warning(
                            "[interface_deep] nested_answer_error_detected output=%s",
                            output.get("output", "未知错误"),
                        )
                        return {
                            "event_type": "chat.error",
                            "error": output.get("output", "未知错误"),
                        }
                    content = (
                        output.get("output", "") if isinstance(output, dict) else str(output)
                    )
                    is_chunked = (
                        output.get("chunked", False) if isinstance(output, dict) else False
                    )
                else:
                    content = str(payload)
                    is_chunked = False

                if not content or not content.strip():
                    return None

                if _has_streamed_content and not is_chunked:
                    # 已通过 chat.delta 流完 → 仅收尾标记，避免下游重复。
                    # has_streamed_content 可能被空白/极小 delta 误置，真实答案
                    # 却只在 answer chunk 里。此时保留正文（answer 处理器会以
                    # chat.delta 补流），保证非 team 前端（只渲染 delta）能看到回复。
                    if _streamed_text and content.strip() in _streamed_text:
                        return {"event_type": "chat.final", "content": ""}
                    streamed_visible = str(_streamed_text or "").strip()
                    # Substantial stream already on the wire this segment —
                    # do not keep final body for drain (would duplicate).
                    if streamed_visible and len(streamed_visible) >= max(
                        8, len(content.strip()) // 4
                    ):
                        return {"event_type": "chat.final", "content": ""}
                    return {"event_type": "chat.final", "content": content}
                if is_chunked:
                    return {"event_type": "chat.delta", "content": content}
                return {"event_type": "chat.final", "content": content}

            if chunk_type == "tool_call":
                tool_info = (
                    payload.get("tool_call", payload) if isinstance(payload, dict) else payload
                )
                # 只透传 stream_source_id：tool 帧带 task_id 会让前端
                # 按它开一个假的 taskRuns 段，且 reasoning 帧与 tool 帧被拆开。
                return _propagate_stream_source_id(
                    {STREAM_SOURCE_ID_FIELD: payload.get(STREAM_SOURCE_ID_FIELD)}
                    if isinstance(payload, dict)
                    else {},
                    {"event_type": "chat.tool_call", "tool_call": tool_info},
                )

            if chunk_type == "tool_update":
                if isinstance(payload, dict):
                    update_info = payload.get("tool_update", payload)
                    update_payload = (
                        dict(update_info)
                        if isinstance(update_info, dict)
                        else {"content": str(update_info)}
                    )
                else:
                    update_payload = {"content": str(payload)}
                return _propagate_stream_source_id(
                    {STREAM_SOURCE_ID_FIELD: payload.get(STREAM_SOURCE_ID_FIELD)}
                    if isinstance(payload, dict)
                    else {},
                    {"event_type": "chat.tool_update", **update_payload},
                )

            if chunk_type == "tool_result":
                if isinstance(payload, dict):
                    result_info = payload.get("tool_result", payload)
                    result_payload = build_tool_result_payload(result_info)
                else:
                    result_payload = {"result": str(payload)}
                return _propagate_stream_source_id(
                    {STREAM_SOURCE_ID_FIELD: payload.get(STREAM_SOURCE_ID_FIELD)}
                    if isinstance(payload, dict)
                    else {},
                    {"event_type": "chat.tool_result", **result_payload},
                )

            if chunk_type == "error":
                error_msg = (
                    payload.get("error", str(payload))
                    if isinstance(payload, dict)
                    else str(payload)
                )
                return {"event_type": "chat.error", "error": error_msg}

            if chunk_type == "retry_notification":
                if isinstance(payload, dict):
                    output = payload.get("output", {})
                    content = output.get("output", "") if isinstance(output, dict) else str(output)
                else:
                    content = str(payload)
                return {
                    "event_type": "chat.delta",
                    "content": content,
                    "source_chunk_type": chunk_type,
                }

            if chunk_type == "security.alert":
                if isinstance(payload, dict):
                    return {
                        "event_type": "security.alert",
                        **payload,
                    }
                return None

            if chunk_type == "chat.retract":
                if isinstance(payload, dict):
                    return {
                        "event_type": "chat.retract",
                        **payload,
                    }
                return None

            # send_file OBS path: OutputSchema(type="chat.file", payload={files:[url...]})
            # must not fall through to _stream_text_payload — files-only payload has
            # no content/output, so the frame would be dropped and Gateway never
            # materializes a download token.
            if chunk_type == "chat.file":
                if isinstance(payload, dict):
                    return {
                        "event_type": "chat.file",
                        **payload,
                    }
                return None

            if chunk_type == "thinking":
                return {
                    "event_type": "chat.processing_status",
                    "is_processing": True,
                    "current_task": "thinking",
                }

            if chunk_type == "todo.updated":
                todos = payload.get("todos", []) if isinstance(payload, dict) else []
                return {"event_type": "todo.updated", "todos": todos}

            if chunk_type == "task.start":
                if isinstance(payload, dict):
                    return {
                        "event_type": "task.start",
                        "task_id": payload.get("task_id"),
                        "task_content": payload.get("task_content"),
                        "task_index": payload.get("task_index"),
                        "total_tasks": payload.get("total_tasks"),
                        "parent_request_id": payload.get("parent_request_id"),
                        "timestamp": payload.get("timestamp"),
                    }
                return None

            if chunk_type == "task.update":
                if isinstance(payload, dict):
                    return {
                        "event_type": "task.update",
                        "tasks": payload.get("tasks", []),
                        "total_tasks": payload.get("total_tasks", 0),
                        "completed_tasks": payload.get("completed_tasks", 0),
                        "in_progress_tasks": payload.get("in_progress_tasks", 0),
                        "pending_tasks": payload.get("pending_tasks", 0),
                        "parent_request_id": payload.get("parent_request_id"),
                        "timestamp": payload.get("timestamp"),
                    }
                return None

            if chunk_type == "task.complete":
                if isinstance(payload, dict):
                    return {
                        "event_type": "task.complete",
                        "task_id": payload.get("task_id"),
                        "task_content": payload.get("task_content"),
                        "status": payload.get("status"),
                        "duration_ms": payload.get("duration_ms"),
                        "error": payload.get("error"),
                        "timestamp": payload.get("timestamp"),
                    }
                return None

            handled, parsed = try_handle_subagent_chunk(
                chunk_type,
                payload,
                parent_session_id=_parent_session_id,
            )
            if handled:
                return parsed

            if chunk_type == "context.usage":
                usage_payload = normalize_context_usage_payload(payload)
                if usage_payload is not None:
                    return usage_payload
                return {"event_type": "context.usage", "rate": 0}

            if chunk_type == "context.compression_state":
                if hasattr(payload, "model_dump"):
                    state_payload = payload.model_dump(mode="json")
                elif isinstance(payload, dict):
                    state_payload = payload
                else:
                    state_payload = {"summary": str(payload)}
                return {
                    "event_type": "context.compression_state",
                    **state_payload,
                }

            if chunk_type == "chat.ask_user_question":
                return parse_ask_user_question_payload(payload)

            if chunk_type == "chat.symphony_status":
                if isinstance(payload, dict):
                    return {
                        "event_type": "chat.symphony_status",
                        **payload,
                    }
                return None

            if chunk_type == "__interaction__":
                if isinstance(payload, dict) and payload.get("interaction_type") == "activate_confirm":
                    return {
                        "event_type": "harness.activate_interaction",
                        "interaction_type": "activate_confirm",
                        "interaction_id": payload.get("interaction_id", ""),
                        "extension_name": payload.get("extension_name", ""),
                        "runtime_path": payload.get("runtime_path", ""),
                        "session_runtime_path": payload.get("session_runtime_path", ""),
                        "extension_runtime_path": payload.get(
                            "extension_runtime_path", payload.get("runtime_path", "")
                        ),
                        "options": payload.get("options", ["accept", "reject"]),
                    }
                return convert_interactions_to_ask_user_question([payload])

            # Auto-harness specific: harness.message event
            if chunk_type == "message":
                content = (
                    payload.get("content", "")
                    if isinstance(payload, dict)
                    else str(payload)
                )
                # Extract stage from payload if available, fallback to _stage parameter
                stage_from_payload = (
                    payload.get("stage", "")
                    if isinstance(payload, dict)
                    else ""
                )
                result: dict[str, Any] = {
                    "event_type": "harness.message",
                    "content": content,
                    "stage": stage_from_payload or _stage,
                }
                # Pass through stages array for dynamic stage definition
                if isinstance(payload, dict):
                    if "stages" in payload:
                        result["stages"] = payload["stages"]
                    if "pipeline" in payload:
                        result["pipeline"] = payload["pipeline"]
                    # Pass through metadata for security alerts and other custom data
                    if "metadata" in payload:
                        result["metadata"] = payload["metadata"]
                return result

            # Auto-harness specific: harness.stage_result event
            if chunk_type == "stage_result":
                if isinstance(payload, dict):
                    stage = payload.get("stage", _stage)
                    return {
                        "event_type": "harness.stage_result",
                        "stage": stage,
                        "status": payload.get("status", "success"),
                        "error": payload.get("error", ""),
                        "messages": payload.get("messages", []),
                        "metrics": payload.get("metrics", {}),
                        "scope": payload.get("scope", ""),
                        "parent_stage": payload.get("parent_stage", ""),
                        "extension_stage": payload.get("extension_stage", ""),
                        "extension_name": payload.get("extension_name", ""),
                        "task_id": payload.get("task_id", ""),
                    }
                return None

            # Auto-harness specific: harness.extension_ready event
            if chunk_type == "extension_ready":
                if isinstance(payload, dict):
                    return {
                        "event_type": "harness.extension_ready",
                        "extension_name": payload.get("extension_name", ""),
                        "runtime_path": payload.get("runtime_path", ""),
                        "session_runtime_path": payload.get("session_runtime_path", ""),
                        "extension_runtime_path": payload.get("extension_runtime_path", ""),
                        "config_path": payload.get("config_path", ""),
                        "runtime_extensions": payload.get("runtime_extensions", []),
                        "verify_report": payload.get("verify_report", {}),
                        "components_summary": payload.get("components_summary", {}),
                    }
                return None

            if chunk_type == "harness_session_finished":
                if isinstance(payload, dict):
                    return {
                        "event_type": "harness.session_finished",
                        "pipeline": payload.get("pipeline", ""),
                        "status": payload.get("status", "success"),
                        "results_count": payload.get("results_count", 0),
                        "is_terminal": bool(payload.get("is_terminal", True)),
                    }
                return {
                    "event_type": "harness.session_finished",
                    "status": "success",
                    "is_terminal": True,
                }

            # Auto-harness specific: activate_testing_guide summary
            if chunk_type == "activate_testing_guide":
                if isinstance(payload, dict):
                    text = payload.get("text", "")
                    if text:
                        return {"event_type": "chat.delta", "content": text}
                return None

            if isinstance(payload, dict):
                if "traceId" in payload or "invokeId" in payload:
                    return None
                content = payload.get("content") or payload.get("output")
            else:
                content = str(payload)
            delta_payload = _stream_text_payload(
                "chat.delta", content
            )
            src = payload if isinstance(payload, dict) else {}
            return _propagate_stream_source_id(src, delta_payload)

        if isinstance(chunk, dict):
            if "traceId" in chunk or "invokeId" in chunk:
                return None
            # Interaction loop lifecycle events emitted by DeepAgent
            # (goal.updated snapshot etc.) forwarded to the TUI goal status bar.
            interaction_evt = chunk.get("type")
            if interaction_evt == GOAL_UPDATED_EVENT_TYPE:
                return _interaction_goal_updated_payload(
                    chunk.get("payload")
                )
            if interaction_evt == ERROR_EVENT_TYPE:
                err_payload = chunk.get("payload")
                err_payload = err_payload if isinstance(err_payload, dict) else {}
                return {
                    "event_type": ERROR_EVENT_TYPE,
                    "code": err_payload.get("code", "execution_error"),
                    "message": err_payload.get("message", "execution error"),
                    "goal": err_payload.get("goal"),
                }
            if chunk.get("result_type") == "error":
                logger.warning(
                    "[interface_deep] top_level_chunk_error_detected output=%s",
                    chunk.get("output", "未知错误"),
                )
                return {
                    "event_type": "chat.error",
                    "error": chunk.get("output", "未知错误"),
                }
            output_payload = chunk.get("output")
            if isinstance(output_payload, dict) and output_payload.get("result_type") == "error":
                logger.warning(
                    "[interface_deep] nested_chunk_error_detected output=%s",
                    output_payload.get("output", "未知错误"),
                )
                return {
                    "event_type": "chat.error",
                    "error": output_payload.get("output", "未知错误"),
                }
            output = chunk.get("output", "")
            if output:
                return {"event_type": "chat.delta", "content": str(output)}
            return None

    except Exception:
        logger.debug("[_parse_stream_chunk] 解析异常", exc_info=True)

    return None
