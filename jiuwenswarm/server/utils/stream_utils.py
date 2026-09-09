# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Stream utilities for parsing agent output chunks."""

from __future__ import annotations

import logging
import math
from typing import Any

logger = logging.getLogger(__name__)


def _model_dump_without_mode(dumper: Any) -> dict[str, Any]:
    """Call a legacy model dumper without passing a mode argument."""
    try:
        result = dumper()
    except (AttributeError, TypeError, ValueError):
        logger.debug("[stream_utils] model serialization failed", exc_info=True)
        return {}
    return result if isinstance(result, dict) else {}


def _model_dump_dict(value: Any, *, mode: str = "json") -> dict[str, Any]:
    """Return a model dump without allowing serialization errors to escape."""
    dumper = getattr(value, "model_dump", None)
    if not callable(dumper):
        return {}
    try:
        result = dumper(mode=mode)
    except TypeError:
        return _model_dump_without_mode(dumper)
    except (AttributeError, ValueError):
        logger.debug("[stream_utils] model serialization failed", exc_info=True)
        return {}
    return result if isinstance(result, dict) else {}


def _finite_number(value: Any) -> int | float | None:
    """Return numeric values that are safe to expose to the frontend."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return value if math.isfinite(float(value)) else None
    except (OverflowError, ValueError):
        return None


def _normalize_occupancy_rate(value: Any) -> float | None:
    """Normalize ratio or percentage values to a ``[0, 1]`` ratio."""
    numeric_value = _finite_number(value)
    if numeric_value is None:
        return None
    occupancy_rate = float(numeric_value)
    if occupancy_rate > 1:
        occupancy_rate /= 100
    return occupancy_rate if 0 <= occupancy_rate <= 1 else None


def normalize_context_usage_payload(payload: Any) -> dict[str, Any] | None:
    """Keep the complete Core context-usage snapshot on the wire.

    Newer ``agent-core`` versions send a ``ContextUsageSnapshot`` containing
    ``context_window`` and per-category ``parts``.  Older Swarm code reduced
    that payload to three legacy fields, which made the category breakdown
    disappear before it reached the Web channel.  Preserve every field and
    only add the legacy aliases still consumed by the existing frontend.
    """
    if hasattr(payload, "model_dump"):
        payload = _model_dump_dict(payload)
    if not isinstance(payload, dict):
        return None

    usage_payload = _serialize_chunk_recursive(dict(payload))
    if not isinstance(usage_payload, dict):
        return None

    usage_payload["event_type"] = "context.usage"
    context_window = usage_payload.get("context_window")
    if isinstance(context_window, dict):
        # Compatibility aliases: the frontend's summary card still reads
        # these names while the full category breakdown is carried in parts.
        occupancy_rate = _normalize_occupancy_rate(
            context_window.get("occupancy_rate")
        )
        if usage_payload.get("rate") is None:
            # Core protocol percentages are ratios in [0, 1], while the
            # legacy frontend alias is displayed as a percentage in [0, 100].
            usage_payload["rate"] = (
                occupancy_rate * 100
                if occupancy_rate is not None
                else 0
            )
        if usage_payload.get("context_max") is None:
            usage_payload["context_max"] = context_window.get("limit_tokens") or 0
        if usage_payload.get("tokens_used") is None:
            usage_payload["tokens_used"] = context_window.get("input_tokens") or 0

        # Keep a small structured summary for the frontend. The display text
        # is rendered by the frontend so it can follow the active locale;
        # the wire payload only carries numeric values.
        raw_summary = usage_payload.get("context_usage_summary")
        summary = dict(raw_summary) if isinstance(raw_summary, dict) else {}
        used_tokens = _finite_number(summary.get("used_tokens"))
        if used_tokens is None:
            used_tokens = _finite_number(context_window.get("input_tokens"))
        limit_tokens = _finite_number(summary.get("limit_tokens"))
        if limit_tokens is None:
            limit_tokens = _finite_number(context_window.get("limit_tokens"))
        summary_rate = _normalize_occupancy_rate(summary.get("occupancy_rate"))
        if summary_rate is None:
            summary_rate = occupancy_rate
        if summary_rate is None and used_tokens is not None:
            if limit_tokens is not None and limit_tokens > 0:
                summary_rate = float(used_tokens) / float(limit_tokens)
        summary["used_tokens"] = used_tokens
        summary["limit_tokens"] = limit_tokens
        summary["occupancy_rate"] = summary_rate
        summary["percentage"] = (
            summary_rate * 100 if summary_rate is not None else None
        )
        usage_payload["context_usage_summary"] = summary
    else:
        for key in ("rate", "context_max", "tokens_used"):
            if usage_payload.get(key) is None:
                usage_payload[key] = 0

    # The Core snapshot exposes the token-weighted session average as a
    # top-level scalar.  Backfill it for older Core payloads that only have
    # the nested aggregate, keeping the frontend protocol version-tolerant.
    if usage_payload.get("session_kv_cache_hit_rate") is None:
        kv_cache = usage_payload.get("kv_cache")
        session_usage = (
            kv_cache.get("session") if isinstance(kv_cache, dict) else None
        )
        if isinstance(session_usage, dict):
            usage_payload["session_kv_cache_hit_rate"] = session_usage.get(
                "weighted_hit_rate"
            )
    return usage_payload


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
        if chunk.get("event_type") == "context.usage":
            usage_payload = normalize_context_usage_payload(chunk)
            if usage_payload is not None:
                return usage_payload
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

    if chunk_type in ("subagent_updated", "subagent_activity", "subagent_message"):
        from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

        return JiuWenSwarmDeepAdapter.parse_stream_chunk(chunk)

    if isinstance(chunk_type, str) and "." in chunk_type:
        if chunk_type == "context.usage":
            usage_payload = normalize_context_usage_payload(payload)
            if usage_payload is not None:
                return usage_payload
        if chunk_type == "context.compression_state":
            if hasattr(payload, "model_dump"):
                payload_dict = _model_dump_dict(payload)
            elif isinstance(payload, dict):
                payload_dict = payload
            else:
                payload_dict = {}
            if payload_dict:
                return {
                    "event_type": "context.compression_state",
                    **payload_dict,
                }
        if isinstance(payload, dict):
            return {
                "event_type": chunk_type,
                **{k: _serialize_chunk_recursive(v) if isinstance(v, (dict, list)) else _serialize_value(v)
                   for k, v in payload.items()},
            }
        if hasattr(payload, "model_dump"):
            payload_dict = _model_dump_dict(payload)
            return {
                "event_type": chunk_type,
                **{k: _serialize_chunk_recursive(v) if isinstance(v, (dict, list)) else _serialize_value(v)
                   for k, v in payload_dict.items()},
            }
        return {"event_type": chunk_type, "content": str(payload)}

    if chunk_type == "controller_output" and payload is not None:
        interactions = _find_interaction_payloads(payload)
        if interactions:
            return _parse_interaction_payload(interactions)
        inner_t = getattr(payload, "type", None)
        if inner_t is None and isinstance(payload, dict):
            inner_t = payload.get("type")
        inner_val = (
            getattr(inner_t, "value", inner_t) if inner_t is not None else None
        )
        if inner_val == "task_completion":
            return None
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

    if chunk_type == "llm_output":
        content = (
            payload.get("content", "")
            if isinstance(payload, dict)
            else str(payload)
        )
        if not content or not content.strip():
            return None
        return {"event_type": "chat.delta", "content": content}

    if chunk_type == "llm_reasoning":
        content = (
            (payload.get("content", "") or payload.get("output", ""))
            if isinstance(payload, dict)
            else str(payload)
        )
        if not content or not content.strip():
            return None
        return {"event_type": "chat.reasoning", "content": content}

    if chunk_type == "content_chunk":
        content = (
            payload.get("content", "")
            if isinstance(payload, dict)
            else str(payload)
        )
        if not content or not content.strip():
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
            return {"event_type": "chat.final", "content": content}
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
                for key in (
                    "success",
                    "status",
                    "is_error",
                    "summary",
                    "graph_status",
                    "graph_build",
                    "direct_display",
                    "display_format",
                    "mermaid",
                ):
                    if key in result_info:
                        result_payload[key] = result_info.get(key)
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
        dumped = _model_dump_dict(obj, mode="python")
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


def _parse_event_typed_chunk(chunk: Any) -> dict[str, Any]:
    """Parse chunk with event_type attribute."""
    if isinstance(chunk, dict):
        return chunk

    result = {"event_type": getattr(chunk, "event_type", "unknown")}
    
    # 优先使用 Pydantic 的 model_dump/dict 方法
    if hasattr(chunk, "model_dump"):
        data = _model_dump_dict(chunk)
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
            if payload.get("event_type") == "context.usage":
                usage_payload = normalize_context_usage_payload(payload)
                if usage_payload is not None:
                    return usage_payload
            return payload

        if "output" in payload:
            result_type = payload.get("result_type", "")
            if result_type == "error":
                return {
                    "event_type": "chat.error",
                    "error": payload.get("output", ""),
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
