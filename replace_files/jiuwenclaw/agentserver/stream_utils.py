# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Stream utilities for parsing agent output chunks."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def tool_calls_payload_to_json_list(raw: Any) -> list[Any]:
    """将流式 payload 中的 tool_calls 规范为 list（OpenJiuwen 已产出 dict，此处只做形状收敛）。"""
    if raw is None:
        return []
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, (list, tuple)):
        return list(raw)
    return []


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
        return {
            "event_type": "chat.delta" if not _has_streamed_content else "chat.final",
            "content": chunk.get("content", ""),
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
        return {
            "event_type": "chat.delta" if not _has_streamed_content else "chat.final",
            "content": chunk.get("output", ""),
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

    if chunk_type == "controller_output" and payload is not None:
        inner_t = getattr(payload, "type", None)
        inner_val = (
            getattr(inner_t, "value", inner_t) if inner_t is not None else None
        )
        if inner_val == "task_completion":
            return None
        if inner_val == "task_failed":
            error = next(
                (item.text for item in payload.data if hasattr(item, "text")),
                "任务执行失败",
            )
            return {"event_type": "chat.error", "error": error}

    if chunk_type == "llm_output":
        content = (
            payload.get("content", "")
            if isinstance(payload, dict)
            else str(payload)
        )
        if not content:
            return None
        return {"event_type": "chat.delta", "content": content}

    if chunk_type == "llm_reasoning":
        content = (
            (payload.get("content", "") or payload.get("output", ""))
            if isinstance(payload, dict)
            else str(payload)
        )
        if not content:
            return None
        return {"event_type": "chat.reasoning", "content": content}

    if chunk_type == "content_chunk":
        content = (
            payload.get("content", "")
            if isinstance(payload, dict)
            else str(payload)
        )
        if not content:
            return None
        return {"event_type": "chat.delta", "content": content}

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

        if _has_streamed_content and not is_chunked:
            # Keep chat.final as a completion marker when the final answer text
            # has already been streamed via chat.delta.
            return {"event_type": "chat.final", "content": ""}

        if not content:
            return None
        if is_chunked:
            return {"event_type": "chat.delta", "content": content}
        return {"event_type": "chat.final", "content": content}

    if chunk_type == "tool_calls.delta":
        if isinstance(payload, dict):
            result: dict[str, Any] = {
                "event_type": "chat.tool_calls.delta",
                "tool_calls": tool_calls_payload_to_json_list(payload.get("tool_calls", [])),
            }
            if "source" in payload:
                result["source"] = payload.get("source")
            return result
        return {
            "event_type": "chat.tool_calls.delta",
            "tool_calls": tool_calls_payload_to_json_list(payload),
        }

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
            result_payload = {
                "result": (
                    result_info.get("result", str(result_info))
                    if isinstance(result_info, dict)
                    else str(result_info)
                ),
            }
            if isinstance(result_info, dict):
                result_payload["tool_name"] = (
                    result_info.get("tool_name") or result_info.get("name")
                )
                result_payload["tool_call_id"] = (
                    result_info.get("tool_call_id") or result_info.get("toolCallId")
                )
                raw_output = result_info.get("raw_output")
                if raw_output is None:
                    raw_output = result_info.get("rawOutput")
                if raw_output is not None:
                    result_payload["raw_output"] = raw_output
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

    if chunk_type == "thinking":
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

    if chunk_type == "context.compressed":
        if isinstance(payload, dict):
            return {
                "event_type": "context.compressed",
                "rate": payload.get("rate", 0),
                "before_compressed": payload.get("before_compressed"),
                "after_compressed": payload.get("after_compressed"),
            }

    if chunk_type == "context.usage":
        if isinstance(payload, dict):
            return {
                "event_type": "context.usage",
                "used_tokens": payload.get("used_tokens"),
                "limit_tokens": payload.get("limit_tokens"),
                "usage_percent": payload.get("usage_percent"),
                "input_tokens": payload.get("input_tokens"),
                "output_tokens": payload.get("output_tokens"),
                "total_tokens": payload.get("total_tokens"),
            }
        return {"event_type": "context.usage"}

    if isinstance(payload, dict):
        if "event_type" in payload:
            if payload.get("event_type") == "chat.tracer_agent":
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
    """将 datetime 等不可 JSON 序列化的对象转换为 JSON 友好格式."""
    from datetime import datetime, date
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
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
