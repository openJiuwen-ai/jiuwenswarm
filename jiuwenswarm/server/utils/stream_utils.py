# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Stream utilities for parsing agent output chunks."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)


def serialize_tool_result_value(result_info: Any) -> Any:
    """Preserve explicit results and JSON-encode a missing-result envelope."""
    if not isinstance(result_info, dict):
        return str(result_info)
    if "result" in result_info:
        return result_info["result"]
    return json.dumps(result_info, ensure_ascii=False, default=str)


def build_tool_result_payload(result_info: Any) -> dict[str, Any]:
    """Build the common ``chat.tool_result`` payload for every adapter path."""
    result_payload = {"result": serialize_tool_result_value(result_info)}
    if not isinstance(result_info, dict):
        return result_payload

    result_payload["tool_name"] = result_info.get("tool_name") or result_info.get("name")
    result_payload["tool_call_id"] = result_info.get("tool_call_id") or result_info.get(
        "toolCallId"
    )

    raw_output = result_info.get("raw_output")
    if raw_output is None:
        raw_output = result_info.get("rawOutput")
    if raw_output is not None:
        result_payload["raw_output"] = raw_output

    for key in (
        "status",
        "success",
        "is_error",
        "error",
        "summary",
        "score_status",
        "score_build",
        "direct_display",
        "display_format",
        "mermaid",
    ):
        if key in result_info:
            result_payload[key] = result_info[key]
    return result_payload


def _propagate_stream_source_id(src_payload: Any) -> dict[str, Any]:
    """从上游 payload 提取 stream_source_id（skill_turbo 并发节点用它标识 source）。

    并发节点（如 p6_1_page_worker）在 llm_reasoning / llm_output 的 payload 里注入
    stream_source_id，前端据其把内容路由到 subagent 行。解析分支重建 payload 时
    必须原样透传，否则并发思考/正文无法分桶、逐字交错。无 source_id 返回空 dict，
    行为与不透传完全一致。
    """
    if isinstance(src_payload, dict):
        source_id = src_payload.get("stream_source_id")
        if source_id:
            return {"stream_source_id": source_id}
    return {}


def parse_stream_chunk(chunk: Any, *, _has_streamed_content: bool = False) -> dict[str, Any] | None:
    """Parse agent output chunk to frontend-consumable payload dict.

    统一处理所有 SDK 输出格式，包括：
    - OutputSchema (type + payload)
    - AgentResponseChunk (request_id + payload)
    - dict (各种格式)
    - 其他对象

    Args:
        chunk: Output chunk from agent runner
        _has_streamed_content: Whether content has been streamed (for backward compatibility)

    Returns:
        Parsed payload dict with event_type, or None if chunk should be skipped
    """
    if chunk is None:
        return None

    if isinstance(chunk, dict):
        return _parse_dict_chunk(chunk, _has_streamed_content)

    if hasattr(chunk, "type") and hasattr(chunk, "payload"):
        return _parse_typed_chunk(chunk, _has_streamed_content)

    if hasattr(chunk, "event_type"):
        return _parse_event_typed_chunk(chunk)

    if hasattr(chunk, "payload") and hasattr(chunk, "request_id"):
        return _parse_response_chunk(chunk, _has_streamed_content)

    return {
        "event_type": "chat.delta",
        "content": str(chunk),
    }


def _parse_dict_chunk(chunk: dict[str, Any], _has_streamed_content: bool) -> dict[str, Any] | None:
    """Parse dict chunk."""
    if "event_type" in chunk:
        if chunk.get("event_type") == "chat.tracer_agent":
            return _serialize_chunk_recursive(chunk)
        return _serialize_chunk_recursive(chunk)

    if "type" in chunk:
        event_type = chunk.get("type")
        if event_type == "tool_call":
            return {
                "event_type": "tool.use",
                **{k: _serialize_value(v) for k, v in chunk.items() if k != "type"},
            }
        if event_type == "tool_result":
            return {
                "event_type": "tool.result",
                **{k: _serialize_value(v) for k, v in chunk.items() if k != "type"},
            }
        return {
            "event_type": event_type,
            **{k: _serialize_value(v) for k, v in chunk.items() if k != "type"},
        }

    if "content" in chunk:
        content = chunk.get("content", "")
        if not content or not content.strip():
            return None
        return {
            "event_type": "chat.delta" if not _has_streamed_content else "chat.final",
            "content": content,
        }

    if "output" in chunk:
        result_type = chunk.get("result_type", "")
        if result_type == "error":
            return {
                "event_type": "chat.error",
                "error": chunk.get("output", ""),
            }
        output = chunk.get("output")
        if isinstance(output, dict) and output.get("result_type") == "error":
            logger.warning(
                "[stream_utils] nested_error_chunk_detected output.result_type=error output=%s",
                output.get("output", ""),
            )
            return {
                "event_type": "chat.error",
                "error": output.get("output", ""),
            }
        output = chunk.get("output", "")
        if not output or not output.strip():
            return None
        return {
            "event_type": "chat.delta" if not _has_streamed_content else "chat.final",
            "content": output,
        }

    return chunk


def _serialize_chunk_recursive(obj: Any) -> Any:
    """递归序列化对象中的 datetime 对象为字符串."""
    if isinstance(obj, dict):
        return {k: _serialize_chunk_recursive(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_serialize_chunk_recursive(x) for x in obj]
    return _serialize_value(obj)


def _parse_typed_chunk(chunk: Any, _has_streamed_content: bool) -> dict[str, Any] | None:
    """Parse OutputSchema-like chunk with type and payload attributes."""
    chunk_type = getattr(chunk, "type", "")
    payload = getattr(chunk, "payload", {})

    if chunk_type == "chat.ask_user_question":
        return parse_ask_user_question_payload(payload)

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

    if isinstance(chunk_type, str) and "." in chunk_type:
        if chunk_type == "context.compression_state":
            if hasattr(payload, "model_dump"):
                try:
                    payload_dict = payload.model_dump(mode="json")
                except Exception:
                    payload_dict = payload.model_dump()
            elif isinstance(payload, dict):
                payload_dict = payload
            else:
                payload_dict = {}
            if payload_dict:
                return {
                    "event_type": "context.compression_state",
                    "status": payload_dict.get("status", ""),
                    "phase": payload_dict.get("phase", ""),
                    "processor": payload_dict.get("processor", ""),
                    "summary": payload_dict.get("summary", ""),
                    "operation_id": payload_dict.get("operation_id", ""),
                }
        if isinstance(payload, dict):
            return {
                "event_type": chunk_type,
                **{k: _serialize_chunk_recursive(v) if isinstance(v, (dict, list)) else _serialize_value(v)
                   for k, v in payload.items()},
            }
        if hasattr(payload, "model_dump"):
            try:
                payload_dict = payload.model_dump(mode="json")
            except Exception:
                payload_dict = payload.model_dump()
            return {
                "event_type": chunk_type,
                **{k: _serialize_chunk_recursive(v) if isinstance(v, (dict, list)) else _serialize_value(v)
                   for k, v in payload_dict.items()},
            }
        return {"event_type": chunk_type, "content": str(payload)}

    if chunk_type == "controller_output" and payload is not None:
        interactions = _find_interaction_payloads(payload)
        if interactions:
            parsed_event = _parse_interaction_payload(interactions)
            if parsed_event is not None:
                return parsed_event
            # interaction payloads were present but none
            # converted — previously this returned None silently and the
            # frontend never received the question.
            logger.warning(
                "[stream_utils] controller_output carried %d interaction payload(s) but none converted to a card",
                len(interactions),
            )
        inner_t = getattr(payload, "type", None)
        if inner_t is None and isinstance(payload, dict):
            inner_t = payload.get("type")
        inner_val = (
            getattr(inner_t, "value", inner_t) if inner_t is not None else None
        )
        if inner_val == "task_failed":
            data = getattr(payload, "data", None)
            if data is None and isinstance(payload, dict):
                data = payload.get("data", [])
            error = next(
                (
                    item.text
                    for item in data
                    if hasattr(item, "text") and str(item.text or "").strip()
                ),
                None,
            )
            if error is None:
                error = next(
                    (
                        str(item.get("text"))
                        for item in data
                        if isinstance(item, dict) and str(item.get("text") or "").strip()
                    ),
                    "任务执行失败",
                )
            return {"event_type": "chat.error", "error": error}
        if inner_val == "task_interaction":
            # native-harness ask_user interrupts (deep-agent
            # task loop) surface as a bare task_interaction controller payload
            # without __interaction__ chunks reaching this stream. Parse the
            # embedded interrupt result; fall back to an awaiting-input card so
            # the frontend is never left waiting without an input affordance.
            parsed_event = parse_task_interaction_payload(payload)
            if parsed_event is not None:
                return parsed_event
            request_id_candidates = _collect_request_id_candidates(payload)
            fallback_rid = request_id_candidates[0] if request_id_candidates else ""
            logger.warning(
                "[stream_utils] task_interaction without parsable ask_user payload;"
                " emitting awaiting-input fallback request_id=%s",
                fallback_rid or "<generated>",
            )
            return _fallback_awaiting_input_card(fallback_rid)
        # Close the enum: do not stringify ControllerOutputPayload as visible text.
        if inner_val not in (
            "task_completion",
            "task_interaction",
            "processing",
            "all_tasks_processed",
        ):
            logger.debug(
                "[stream_utils] drop unhandled controller_output type=%r",
                inner_val,
            )
        return None

    if chunk_type == "llm_output":
        # openjiuwen streams payload.output; SkillTurbo uses payload.content.
        content = (
            (payload.get("content", "") or payload.get("output", ""))
            if isinstance(payload, dict)
            else str(payload)
        )
        if not content or not content.strip():
            return None
        return {
            "event_type": "chat.delta",
            "content": content,
            **_propagate_stream_source_id(payload),
        }

    if chunk_type == "llm_reasoning":
        content = (
            (payload.get("content", "") or payload.get("output", ""))
            if isinstance(payload, dict)
            else str(payload)
        )
        if not content or not content.strip():
            return None
        return {
            "event_type": "chat.reasoning",
            "content": content,
            **_propagate_stream_source_id(payload),
        }

    if chunk_type == "content_chunk":
        content = (
            payload.get("content", "")
            if isinstance(payload, dict)
            else str(payload)
        )
        if not content or not content.strip():
            return None
        return {
            "event_type": "chat.delta",
            "content": content,
            **_propagate_stream_source_id(payload),
        }

    if chunk_type == "answer":
        if isinstance(payload, dict):
            if payload.get("result_type") == "error":
                return {
                    "event_type": "chat.error",
                    "error": payload.get("output", "未知错误"),
                }
            output = payload.get("output", {})
            if isinstance(output, dict) and output.get("result_type") == "error":
                logger.warning(
                    "[stream_utils] nested_answer_error_detected output.result_type=error output=%s",
                    output.get("output", "未知错误"),
                )
                return {
                    "event_type": "chat.error",
                    "error": output.get("output", "未知错误"),
                }
            content = (
                output.get("output", "")
                if isinstance(output, dict)
                else str(output)
            )
            is_chunked = (
                output.get("chunked", False)
                if isinstance(output, dict)
                else False
            )
        else:
            content = str(payload)
            is_chunked = False

        if not content or not content.strip():
            return None

        if _has_streamed_content and not is_chunked:
            # Keep chat.final as a completion marker when the final answer text
            # has already been streamed via chat.delta.
            return {"event_type": "chat.final", "content": ""}
        if is_chunked:
            return {"event_type": "chat.delta", "content": content}
        return {"event_type": "chat.final", "content": content}

    if chunk_type == "tool_call":
        tool_info = (
            payload.get("tool_call", payload)
            if isinstance(payload, dict)
            else payload
        )
        return {"event_type": "chat.tool_call", "tool_call": tool_info}

    if chunk_type == "tool_update":
        if isinstance(payload, dict):
            update_info = payload.get("tool_update", payload)
            update_payload = dict(update_info) if isinstance(update_info, dict) else {"content": str(update_info)}
        else:
            update_payload = {"content": str(payload)}
        return {
            "event_type": "chat.tool_update",
            **update_payload,
        }

    if chunk_type == "tool_result":
        if isinstance(payload, dict):
            result_info = payload.get("tool_result", payload)
            result_payload = build_tool_result_payload(result_info)
        else:
            result_payload = {"result": str(payload)}
        return {
            "event_type": "chat.tool_result",
            **result_payload,
        }

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

    if chunk_type in ("thinking", "llm_toolcall_progress"):
        # `thinking`: model 处理中（既有）。
        # `llm_toolcall_progress`: tool_call 长流式期间 react_agent 节流发的心跳
        # （本修复新增）。两者都映射为业务帧 chat.processing_status——relay 看门狗
        # 计为业务帧重置 300s；前端静默 setAgentStatus(streaming)。
        # 不设 is_complete——避免触发关流启发式。
        return {
            "event_type": "chat.processing_status",
            "is_processing": True,
            "current_task": "thinking",
        }

    if chunk_type == "todo.updated":
        todos = (
            payload.get("todos", [])
            if isinstance(payload, dict)
            else []
        )
        return {"event_type": "todo.updated", "todos": todos}

    if chunk_type == "context.usage":
        if isinstance(payload, dict):
            usage_payload = {
                "event_type": "context.usage",
                "rate": payload.get("rate", 0),
                "context_max": payload.get("context_max") or 0,
                "tokens_used": payload.get("tokens_used") or 0,
            }
            for key in ("role", "member_name"):
                value = payload.get(key)
                if value is not None:
                    usage_payload[key] = value
            return usage_payload

    if chunk_type == "chat.retract":
        if isinstance(payload, dict):
            return {
                "event_type": "chat.retract",
                **{k: v for k, v in payload.items()},
            }
        return None

    if chunk_type == "__interaction__":
        return _parse_interaction_payload(payload)

    if isinstance(payload, dict):
        if "event_type" in payload:
            inner_event = payload.get("event_type")
            # Team-level control events (team.runtime_ready, team.completed)
            # carry their own event_type namespace — pass through as-is
            # rather than wrapping under "chat.{chunk_type}".
            if isinstance(inner_event, str) and inner_event.startswith("team."):
                return {
                    **{k: _serialize_value(v) for k, v in payload.items()},
                }
            if inner_event == "chat.tracer_agent":
                return {
                    "event_type": f"chat.{chunk_type}",
                    **{k: _serialize_chunk_recursive(v) if isinstance(v, (dict, list)) else _serialize_value(v)
                       for k, v in payload.items()},
                }
            return {
                "event_type": f"chat.{chunk_type}",
                **{k: _serialize_value(v) for k, v in payload.items()},
            }
        return {
            "event_type": f"chat.{chunk_type}",
            **{k: _serialize_value(v) for k, v in payload.items()},
        }

    return {
        "event_type": f"chat.{chunk_type}",
        "content": str(payload),
    }


def parse_ask_user_question_payload(payload: Any) -> dict[str, Any]:
    question_payload = payload if isinstance(payload, dict) else {}
    question_payload = dict(question_payload)
    evolution_meta = question_payload.get("evolution_meta")
    legacy_evolution_meta = question_payload.get("_evolution_meta")
    if not isinstance(evolution_meta, dict) and isinstance(legacy_evolution_meta, dict):
        question_payload["evolution_meta"] = dict(legacy_evolution_meta)
    question_payload.pop("_evolution_meta", None)
    return {
        "event_type": "chat.ask_user_question",
        **question_payload,
    }


def _parse_interaction_payload(payload: Any) -> dict[str, Any] | None:
    """Convert a Core interaction payload into a frontend ask-user event."""
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
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        convert_interactions_to_ask_user_question,
    )

    return convert_interactions_to_ask_user_question([payload])


def _find_interaction_payloads(
    obj: Any,
    *,
    _depth: int = 0,
    _seen: set[int] | None = None,
) -> list[Any]:
    """Find nested ``__interaction__`` payloads inside controller output."""
    if obj is None or _depth > 8:
        return []
    if _seen is None:
        _seen = set()
    obj_id = id(obj)
    if obj_id in _seen:
        return []
    _seen.add(obj_id)

    obj_type = getattr(obj, "type", None)
    if obj_type == "__interaction__":
        return [getattr(obj, "payload", None)]

    if isinstance(obj, dict):
        if obj.get("type") == "__interaction__":
            return [obj.get("payload")]
        if obj.get("event_type") == "chat.ask_user_question":
            return [{
                "id": obj.get("request_id", ""),
                "value": {"questions": obj.get("questions", [])},
            }]
        found: list[Any] = []
        for value in obj.values():
            found.extend(_find_interaction_payloads(value, _depth=_depth + 1, _seen=_seen))
        return found

    if isinstance(obj, (list, tuple)):
        found: list[Any] = []
        for value in obj:
            found.extend(_find_interaction_payloads(value, _depth=_depth + 1, _seen=_seen))
        return found

    if hasattr(obj, "model_dump"):
        try:
            dumped = obj.model_dump(mode="python")
        except Exception:
            dumped = obj.model_dump()
        return _find_interaction_payloads(dumped, _depth=_depth + 1, _seen=_seen)

    found: list[Any] = []
    for attr_name in ("payload", "data", "value", "result"):
        if hasattr(obj, attr_name):
            found.extend(_find_interaction_payloads(
                getattr(obj, attr_name),
                _depth=_depth + 1,
                _seen=_seen,
            ))
    return found


def _find_interaction_payload(
    obj: Any,
    *,
    _depth: int = 0,
    _seen: set[int] | None = None,
) -> Any | None:
    """Find a nested ``__interaction__`` payload inside controller output."""
    matches = _find_interaction_payloads(obj, _depth=_depth, _seen=_seen)
    return matches[0] if matches else None


def _dump_model(obj: Any) -> Any:
    """Best-effort ``model_dump`` for pydantic objects, else the object."""
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump(mode="python")
        except Exception:
            try:
                return obj.model_dump()
            except Exception:
                return obj
    return obj


def _iter_ask_user_interrupt_values(
    obj: Any,
    *,
    _depth: int = 0,
    _seen: set[int] | None = None,
):
    """Yield dicts that look like ask_user interrupt values inside ``obj``.

    Native-harness ask_user interrupts (deep-agent task loop) embed the
    interrupt value somewhere inside the task-loop result carried by the
    ``task_interaction`` controller payload, e.g. a dict with
    ``tool_name == "ask_user"`` or a ToolCallInterruptRequest dump with
    ``payload_schema`` + ``questions``.
    """
    if obj is None or _depth > 10:
        return
    if _seen is None:
        _seen = set()
    obj_id = id(obj)
    if obj_id in _seen:
        return
    _seen.add(obj_id)

    if isinstance(obj, dict):
        tool_name = str(obj.get("tool_name") or "").strip()
        if tool_name == "ask_user" or (
            "payload_schema" in obj and "questions" in obj
        ):
            yield obj
        for value in obj.values():
            yield from _iter_ask_user_interrupt_values(
                value, _depth=_depth + 1, _seen=_seen
            )
        return

    if isinstance(obj, (list, tuple)):
        for value in obj:
            yield from _iter_ask_user_interrupt_values(
                value, _depth=_depth + 1, _seen=_seen
            )
        return

    dumped = _dump_model(obj)
    if dumped is not obj:
        yield from _iter_ask_user_interrupt_values(
            dumped, _depth=_depth + 1, _seen=_seen
        )
        return

    for attr_name in ("payload", "data", "value", "result", "state", "output"):
        if hasattr(obj, attr_name):
            yield from _iter_ask_user_interrupt_values(
                getattr(obj, attr_name), _depth=_depth + 1, _seen=_seen
            )


def _collect_request_id_candidates(
    obj: Any,
    *,
    _depth: int = 0,
    _seen: set[int] | None = None,
) -> list[str]:
    """Collect plausible interaction request ids (tool_call_id et al)."""
    ids: list[str] = []
    if obj is None or _depth > 10:
        return ids
    if _seen is None:
        _seen = set()
    obj_id = id(obj)
    if obj_id in _seen:
        return ids
    _seen.add(obj_id)

    if isinstance(obj, dict):
        for key in ("tool_call_id", "request_id", "interrupt_id"):
            value = obj.get(key)
            if isinstance(value, str) and value.strip():
                ids.append(value.strip())
        interrupt_ids = obj.get("interrupt_ids")
        if isinstance(interrupt_ids, (list, tuple)):
            for value in interrupt_ids:
                if isinstance(value, str) and value.strip():
                    ids.append(value.strip())
        for value in obj.values():
            ids.extend(
                _collect_request_id_candidates(value, _depth=_depth + 1, _seen=_seen)
            )
        return ids

    if isinstance(obj, (list, tuple)):
        for value in obj:
            ids.extend(
                _collect_request_id_candidates(value, _depth=_depth + 1, _seen=_seen)
            )
        return ids

    dumped = _dump_model(obj)
    if dumped is not obj:
        return _collect_request_id_candidates(dumped, _depth=_depth + 1, _seen=_seen)
    return ids


def _ask_user_value_has_structured_questions(value_obj: dict[str, Any]) -> bool:
    """Whether the interrupt value carries a structured ``questions`` payload."""
    questions = value_obj.get("questions")
    if isinstance(questions, list) and questions:
        return True
    tool_args = value_obj.get("tool_args")
    if isinstance(tool_args, str):
        try:
            tool_args = json.loads(tool_args)
        except (ValueError, TypeError):
            tool_args = None
    return (
        isinstance(tool_args, dict)
        and isinstance(tool_args.get("questions"), list)
        and bool(tool_args["questions"])
    )


def _resolve_task_interaction_request_id(
    value_obj: dict[str, Any],
    request_id_candidates: list[str],
) -> str:
    """Pick the request id that resumes the pending interrupt (tool_call_id)."""
    for source in (value_obj, value_obj.get("tool_args")):
        if not isinstance(source, dict):
            continue
        for key in ("tool_call_id", "request_id", "interrupt_id"):
            value = str(source.get(key) or "").strip()
            if value:
                return value
    return request_id_candidates[0] if request_id_candidates else ""


def parse_task_interaction_payload(payload: Any) -> dict[str, Any] | None:
    """Build a frontend ask-user card from a TASK_INTERACTION controller payload.

    Rail-based interrupts already expose ``__interaction__`` payloads which
    ``_find_interaction_payloads`` picks up; this handles the remaining
    native-harness shapes so the ask_user question still reaches the frontend.
    Returns ``None`` when no ask_user value is embedded.
    """
    candidates = [
        value
        for value in _iter_ask_user_interrupt_values(payload)
        if isinstance(value, dict)
    ]
    if not candidates:
        return None

    request_id_candidates = _collect_request_id_candidates(payload)
    if not request_id_candidates:
        request_id_candidates = [f"task_interaction_{uuid.uuid4().hex[:12]}"]

    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        convert_interactions_to_ask_user_question,
    )

    # Prefer structured question payloads, then plain query interrupts.
    candidates.sort(
        key=lambda v: 0 if _ask_user_value_has_structured_questions(v) else 1
    )
    for value_obj in candidates:
        request_id = _resolve_task_interaction_request_id(
            value_obj, request_id_candidates
        )
        try:
            card = convert_interactions_to_ask_user_question(
                [{"id": request_id, "value": value_obj}]
            )
        except Exception:
            logger.exception(
                "[stream_utils] failed to convert task_interaction value to ask_user card"
            )
            card = None
        if card is not None:
            return card
    return None


def _fallback_awaiting_input_card(request_id: str) -> dict[str, Any]:
    """Build a generic awaiting-input card for unparsable task_interaction."""
    return {
        "event_type": "chat.ask_user_question",
        "request_id": request_id or f"task_interaction_{uuid.uuid4().hex[:12]}",
        "questions": [
            {
                "question": (
                    "任务正在等待你的输入后才能继续，请直接回复；"
                    "若此前的问题未显示，请重新描述你的需求。"
                ),
                "header": "Input required",
                "options": [],
                "multi_select": False,
            }
        ],
        "source": "task_interaction_fallback",
    }


def _parse_event_typed_chunk(chunk: Any) -> dict[str, Any]:
    """Parse chunk with event_type attribute."""
    if isinstance(chunk, dict):
        return chunk

    result = {"event_type": getattr(chunk, "event_type", "unknown")}
    
    # 优先使用 Pydantic 的 model_dump/dict 方法
    if hasattr(chunk, "model_dump"):
        # Pydantic v2 - mode='json' 会将 datetime 转换为 ISO 格式字符串
        try:
            data = chunk.model_dump(mode="json")
        except Exception:
            # 如果 mode='json' 失败，回退到默认模式并手动序列化
            data = chunk.model_dump()
            data = {k: _serialize_value(v) for k, v in data.items()}
        result.update({k: v for k, v in data.items() if k != "event_type"})
    elif hasattr(chunk, "dict"):
        # Pydantic v1
        data = chunk.dict()
        result.update({k: _serialize_value(v) for k, v in data.items() if k != "event_type"})
    elif hasattr(chunk, "__dict__"):
        result.update({k: _serialize_value(v) for k, v in chunk.__dict__.items() if k != "event_type"})
    return result


def _serialize_value(value: Any) -> Any:
    """Serialize non-JSON-native values to frontend-safe payloads."""
    from datetime import date, datetime
    from enum import Enum

    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    return value


def _parse_response_chunk(chunk: Any, _has_streamed_content: bool) -> dict[str, Any] | None:
    """Parse AgentResponseChunk-like object."""
    payload = getattr(chunk, "payload", None)

    if isinstance(payload, dict):
        if "event_type" in payload:
            return payload

        if "output" in payload:
            result_type = payload.get("result_type", "")
            if result_type == "error":
                return {
                    "event_type": "chat.error",
                    "error": payload.get("output", ""),
                }
            output = payload.get("output")
            if isinstance(output, dict) and output.get("result_type") == "error":
                logger.warning(
                    "[stream_utils] nested_response_error_detected output.result_type=error output=%s",
                    output.get("output", ""),
                )
                return {
                    "event_type": "chat.error",
                    "error": output.get("output", ""),
                }
            return {
                "event_type": "chat.delta" if not _has_streamed_content else "chat.final",
                "content": payload.get("output", ""),
            }

        if "content" in payload:
            return {
                "event_type": "chat.delta" if not _has_streamed_content else "chat.final",
                "content": payload.get("content", ""),
            }

        return payload

    return {
        "event_type": "chat.delta",
        "content": str(payload) if payload else "",
    }
