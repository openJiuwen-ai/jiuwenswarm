from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import httpx
import uvicorn


_TRACE_CONTEXT_KEYS = {
    "project_dir",
    "project_id",
    "workspace_dir",
    "workspace",
    "cwd",
    "workdir",
    "task_workspace",
    "task_cwd",
    "session_id",
}
_TRACE_TOOL_ARGUMENT_KEYS = _TRACE_CONTEXT_KEYS | {"path", "command", "description"}
_TRACE_CONTEXT_LABELS = (
    "当前项目目录",
    "项目目录",
    "当前任务根目录",
    "临时工作目录",
    "最终产物目录",
    "工作目录",
    "project_dir",
    "workspace_dir",
    "workspace",
    "workdir",
    "cwd",
)
_TRACE_ENV_KEYS = (
    "PWD",
    "CD",
    "INIT_CWD",
    "JIUSWARM_SANDBOX_PROJECT_DIR",
    "JIUSWARM_PROJECT_DIR",
    "JIUSWARM_WORKSPACE_DIR",
    "JIUWENSWARM_PROJECT_DIR",
    "JIUWENSWARM_WORKSPACE_DIR",
)
_TRACE_MAX_ITEMS = 64
_TRACE_MAX_TEXT_LENGTH = 2_048


def utc_now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


def _decode_json_bytes(raw: bytes) -> Any:
    if not raw:
        return None
    return json.loads(raw.decode("utf-8"))


def _truncate_trace_text(value: str, limit: int = _TRACE_MAX_TEXT_LENGTH) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    return f"{value[:limit]}…"


def _normalise_trace_key(key: Any) -> str:
    key_text = str(key).replace("-", "_")
    key_text = re.sub(r"(?<!^)(?=[A-Z])", "_", key_text)
    return key_text.lower()


def _iter_text_fragments(
    value: Any, location: str = "$", depth: int = 0
) -> Iterator[tuple[str, str]]:
    if depth > 10:
        return
    if isinstance(value, str):
        yield location, value
        return
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _iter_text_fragments(child, f"{location}.{key}", depth + 1)
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            yield from _iter_text_fragments(child, f"{location}[{index}]", depth + 1)


def _extract_context_fields(value: Any) -> dict[str, list[Any]]:
    fields: dict[str, list[Any]] = {}

    def add(key: str, item: Any) -> None:
        if (
            not isinstance(item, (str, int, float, bool))
            or len(fields.get(key, [])) >= _TRACE_MAX_ITEMS
        ):
            return
        values = fields.setdefault(key, [])
        if item not in values:
            values.append(item)

    def visit(item: Any, depth: int = 0) -> None:
        if depth > 10:
            return
        if isinstance(item, dict):
            for key, child in item.items():
                normalised_key = _normalise_trace_key(key)
                if normalised_key in _TRACE_CONTEXT_KEYS:
                    add(normalised_key, child)
                visit(child, depth + 1)
            return
        if isinstance(item, list):
            for child in item:
                visit(child, depth + 1)
            return
        if isinstance(item, str):
            stripped = item.lstrip()
            if len(stripped) > 64_000 or stripped[:1] not in "[{":
                return
            try:
                visit(json.loads(stripped), depth + 1)
            except (TypeError, ValueError, json.JSONDecodeError):
                return

    visit(value)
    return fields


def _looks_like_path(text: str) -> bool:
    return bool(
        re.search(r"[A-Za-z]:[\\/]|/(?:Users|home|tmp|var|opt|workspace)/", text)
    )


def _extract_workspace_mentions(value: Any) -> list[dict[str, Any]]:
    mentions: list[dict[str, Any]] = []
    labels = tuple(label.casefold() for label in _TRACE_CONTEXT_LABELS)
    for location, text in _iter_text_fragments(value):
        for line_number, raw_line in enumerate(text.splitlines(), start=1):
            line = raw_line.strip()
            lower_line = line.casefold()
            if not line or not any(label in lower_line for label in labels):
                continue
            if (
                not _looks_like_path(line)
                and "目录" not in line
                and "workspace" not in lower_line
            ):
                continue
            mentions.append(
                {
                    "location": location,
                    "line_number": line_number,
                    "line": _truncate_trace_text(line),
                }
            )
            if len(mentions) >= _TRACE_MAX_ITEMS:
                return mentions
    return mentions


def _parse_tool_arguments(raw_arguments: Any) -> Any:
    if isinstance(raw_arguments, dict):
        return raw_arguments
    if not isinstance(raw_arguments, str):
        return raw_arguments
    try:
        return json.loads(raw_arguments)
    except (TypeError, ValueError, json.JSONDecodeError):
        return _truncate_trace_text(raw_arguments)


def _summarize_tool_call(call: Any, location: str) -> dict[str, Any] | None:
    if not isinstance(call, dict):
        return None
    function = call.get("function")
    if not isinstance(function, dict) or "arguments" not in function:
        return None

    arguments = _parse_tool_arguments(function.get("arguments"))
    summary: dict[str, Any] = {
        "location": location,
        "id": call.get("id"),
        "name": function.get("name"),
    }
    if isinstance(arguments, dict):
        summary["argument_keys"] = sorted(str(key) for key in arguments)
        if str(function.get("name") or "").casefold() in {
            "bash",
            "shell",
            "powershell",
            "mcp_exec_command",
        }:
            summary["working_directory_arg_present"] = any(
                _normalise_trace_key(key) in {"workdir", "cwd"} for key in arguments
            )
        for key, value in arguments.items():
            if _normalise_trace_key(key) not in _TRACE_TOOL_ARGUMENT_KEYS:
                continue
            if isinstance(value, str):
                summary[_normalise_trace_key(key)] = _truncate_trace_text(value)
            elif isinstance(value, (int, float, bool)) or value is None:
                summary[_normalise_trace_key(key)] = value
            else:
                summary[_normalise_trace_key(key)] = _truncate_trace_text(
                    json.dumps(value, ensure_ascii=False)
                )
    else:
        summary["arguments_preview"] = _truncate_trace_text(str(arguments))
    return summary


def _summarize_tool_calls(messages: Any) -> list[dict[str, Any]]:
    if not isinstance(messages, list):
        return []
    summaries: list[dict[str, Any]] = []
    for message_index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for call_index, call in enumerate(tool_calls):
            summary = _summarize_tool_call(
                call,
                f"messages[{message_index}].tool_calls[{call_index}]",
            )
            if summary is not None:
                summaries.append(summary)
            if len(summaries) >= _TRACE_MAX_ITEMS:
                return summaries
    return summaries


def _safe_trace_headers(headers: dict[str, str]) -> dict[str, str]:
    redacted_markers = (
        "authorization",
        "cookie",
        "token",
        "api-key",
        "apikey",
        "secret",
    )
    result: dict[str, str] = {}
    for key, value in headers.items():
        lower_key = key.casefold()
        result[key] = (
            "<redacted>"
            if any(marker in lower_key for marker in redacted_markers)
            else _truncate_trace_text(value, limit=512)
        )
    return result


def _proxy_runtime_snapshot() -> dict[str, Any]:
    try:
        current_cwd = os.getcwd()
    except OSError:
        current_cwd = None
    try:
        path_cwd = str(Path.cwd())
    except OSError:
        path_cwd = None
    return {
        "pid": os.getpid(),
        "cwd": current_cwd,
        "path_cwd": path_cwd,
        "environment": {
            key: os.environ[key]
            for key in _TRACE_ENV_KEYS
            if os.environ.get(key) is not None
        },
    }


def _decode_sse_event(event: bytes) -> Any:
    data_lines: list[str] = []
    for raw_line in event.splitlines():
        if raw_line.startswith(b"data:"):
            data_lines.append(raw_line[5:].lstrip().decode("utf-8", errors="replace"))
    if not data_lines:
        return None
    data = "\n".join(data_lines).strip()
    if not data or data == "[DONE]":
        return None
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        return None


class StreamingResponseAccumulator:
    """Reconstruct assistant messages from OpenAI-compatible SSE chunks."""

    def __init__(self) -> None:
        self._choices: dict[int, dict[str, Any]] = {}
        self._event_count = 0
        self._event_types: dict[str, int] = {}

    def add_payload(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        self._event_count += 1
        object_type = str(payload.get("object") or payload.get("type") or "unknown")
        self._event_types[object_type] = self._event_types.get(object_type, 0) + 1
        choices = payload.get("choices")
        if not isinstance(choices, list):
            return

        for choice in choices:
            if not isinstance(choice, dict):
                continue
            try:
                choice_index = int(choice.get("index", 0))
            except (TypeError, ValueError):
                choice_index = 0
            state = self._choices.setdefault(
                choice_index,
                {
                    "role": "assistant",
                    "content": [],
                    "reasoning_content": [],
                    "refusal": [],
                    "tool_calls": {},
                    "finish_reason": None,
                },
            )
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                delta = {}
            if isinstance(delta.get("role"), str):
                state["role"] = delta["role"]
            for field in ("content", "reasoning_content", "refusal"):
                value = delta.get(field)
                if isinstance(value, str):
                    state[field].append(value)
            tool_calls = delta.get("tool_calls")
            if isinstance(tool_calls, list):
                for fallback_index, tool_call_delta in enumerate(tool_calls):
                    if not isinstance(tool_call_delta, dict):
                        continue
                    raw_index = tool_call_delta.get("index", fallback_index)
                    try:
                        tool_index = int(raw_index)
                    except (TypeError, ValueError):
                        tool_index = fallback_index
                    call_state = state["tool_calls"].setdefault(
                        tool_index,
                        {
                            "id": None,
                            "type": "function",
                            "name": "",
                            "arguments": [],
                        },
                    )
                    if isinstance(tool_call_delta.get("id"), str):
                        call_state["id"] = tool_call_delta["id"]
                    if isinstance(tool_call_delta.get("type"), str):
                        call_state["type"] = tool_call_delta["type"]
                    function = tool_call_delta.get("function")
                    if not isinstance(function, dict):
                        continue
                    if isinstance(function.get("name"), str):
                        call_state["name"] += function["name"]
                    if isinstance(function.get("arguments"), str):
                        call_state["arguments"].append(function["arguments"])
            if choice.get("finish_reason") is not None:
                state["finish_reason"] = choice["finish_reason"]

    def messages(self) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        for choice_index in sorted(self._choices):
            state = self._choices[choice_index]
            message: dict[str, Any] = {
                "role": state["role"],
                "content": "".join(state["content"]) or None,
            }
            for field in ("reasoning_content", "refusal"):
                value = "".join(state[field])
                if value:
                    message[field] = value
            tool_calls: list[dict[str, Any]] = []
            for tool_index in sorted(state["tool_calls"]):
                call_state = state["tool_calls"][tool_index]
                tool_calls.append(
                    {
                        "id": call_state["id"],
                        "type": call_state["type"],
                        "function": {
                            "name": call_state["name"],
                            "arguments": "".join(call_state["arguments"]),
                        },
                    }
                )
            if tool_calls:
                message["tool_calls"] = tool_calls
            messages.append(message)
        return messages

    def diagnostics(self) -> dict[str, Any]:
        return {
            "sse_event_count": self._event_count,
            "sse_event_types": self._event_types,
            "finish_reasons": {
                str(index): state["finish_reason"]
                for index, state in self._choices.items()
                if state["finish_reason"] is not None
            },
        }


def _prepare_request_body(content_type: str, body: bytes) -> tuple[bytes, Any]:
    if not content_type.startswith("application/json"):
        return body, None

    try:
        request_json = _decode_json_bytes(body)
    except Exception:
        return body, None

    if isinstance(request_json, dict) and request_json.get("stream") is True:
        stream_options = request_json.get("stream_options")
        if not isinstance(stream_options, dict):
            stream_options = {}
        if stream_options.get("include_usage") is not True:
            stream_options = dict(stream_options)
            stream_options["include_usage"] = True
            request_json = dict(request_json)
            request_json["stream_options"] = stream_options
            body = json.dumps(request_json, ensure_ascii=False).encode("utf-8")

    return body, request_json


def _zero_usage() -> dict[str, Any]:
    return {
        "input_tokens": 0,
        "total_input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cached_tokens": None,
        "cache_creation_tokens": None,
        "cache_miss_tokens": None,
        "cache_rate": None,
        "original_input_tokens": None,
        "compression_ratio": None,
        "compression_rate": None,
        "kv_cache_hit_tokens": None,
        "kv_cache_miss_tokens": None,
        "kv_cache_creation_tokens": None,
        "kv_cache_hit_rate": None,
        "raw_usage": None,
    }


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _get_nested_int(payload: dict[str, Any], path: tuple[str, ...]) -> int | None:
    current: Any = payload
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return _to_int(current)


def _first_nested_int(
    payload: dict[str, Any], paths: list[tuple[str, ...]]
) -> int | None:
    for path in paths:
        value = _get_nested_int(payload, path)
        if value is not None:
            return value
    return None


def _safe_ratio(numerator: int | None, denominator: int | None) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator


def _extract_usage(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return _zero_usage()

    # Anthropic streaming events put usage under ``message.usage`` in the
    # ``message_start`` event, while the final ``message_delta`` event puts
    # it at the top level. Accept both shapes for the transparent proxy.
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        message = payload.get("message")
        usage = message.get("usage") if isinstance(message, dict) else None
    if not isinstance(usage, dict):
        return _zero_usage()

    input_tokens = usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0
    output_tokens = usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0
    input_tokens = int(input_tokens)
    output_tokens = int(output_tokens)

    cached_tokens = _first_nested_int(
        usage,
        [
            ("cached_tokens",),
            ("cache_hit_tokens",),
            ("prompt_cache_hit_tokens",),
            ("cache_read_input_tokens",),
            ("prompt_tokens_details", "cached_tokens"),
            ("input_tokens_details", "cached_tokens"),
        ],
    )
    cache_creation_tokens = _first_nested_int(
        usage,
        [
            ("cache_creation_tokens",),
            ("cache_creation_input_tokens",),
            ("prompt_cache_creation_tokens",),
        ],
    )
    cache_miss_tokens = _first_nested_int(
        usage,
        [
            ("cache_miss_tokens",),
            ("prompt_cache_miss_tokens",),
        ],
    )
    separate_cache_tokens = (
        "cache_read_input_tokens" in usage or "cache_creation_input_tokens" in usage
    )
    if cache_miss_tokens is None:
        if separate_cache_tokens:
            # Anthropic reports uncached input separately from cache reads.
            cache_miss_tokens = input_tokens
        elif cached_tokens is not None:
            # DeepSeek reports hit/miss as a split of prompt_tokens.
            cache_miss_tokens = max(0, input_tokens - cached_tokens)

    total_input_tokens = input_tokens
    if separate_cache_tokens:
        total_input_tokens += cached_tokens or 0
        total_input_tokens += cache_creation_tokens or 0

    total_tokens = usage.get("total_tokens", total_input_tokens + output_tokens) or 0
    total_tokens = int(total_tokens)

    original_input_tokens = _first_nested_int(
        usage,
        [
            ("original_input_tokens",),
            ("raw_input_tokens",),
            ("uncompressed_input_tokens",),
            ("pre_compression_input_tokens",),
            ("prompt_tokens_before_compression",),
        ],
    )
    compression_ratio = _safe_ratio(input_tokens, original_input_tokens)
    compression_rate = None if compression_ratio is None else 1 - compression_ratio

    return {
        "input_tokens": input_tokens,
        "total_input_tokens": total_input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cached_tokens": cached_tokens,
        "cache_creation_tokens": cache_creation_tokens,
        "cache_miss_tokens": cache_miss_tokens,
        "cache_rate": _safe_ratio(cached_tokens, total_input_tokens),
        "original_input_tokens": original_input_tokens,
        "compression_ratio": compression_ratio,
        "compression_rate": compression_rate,
        # Explicit aliases make KV-cache metrics easy to query regardless of
        # whether the provider calls them cached/read input tokens.
        "kv_cache_hit_tokens": cached_tokens,
        "kv_cache_miss_tokens": cache_miss_tokens,
        "kv_cache_creation_tokens": cache_creation_tokens,
        "kv_cache_hit_rate": _safe_ratio(cached_tokens, total_input_tokens),
        "raw_usage": usage,
    }


def _merge_usage(current: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    """Merge partial usage updates emitted by a streaming response."""
    merged = dict(current)
    counter_keys = (
        "input_tokens",
        "total_input_tokens",
        "output_tokens",
        "total_tokens",
        "cached_tokens",
        "cache_creation_tokens",
        "cache_miss_tokens",
    )
    for key in counter_keys:
        value = update.get(key)
        if value is None:
            continue
        old_value = merged.get(key)
        merged[key] = max(int(old_value or 0), int(value or 0))

    if isinstance(current.get("raw_usage"), dict) and isinstance(
        update.get("raw_usage"), dict
    ):
        merged["raw_usage"] = {**current["raw_usage"], **update["raw_usage"]}
    elif update.get("raw_usage") is not None:
        merged["raw_usage"] = update["raw_usage"]

    raw_usage = merged.get("raw_usage")
    separate_cache_tokens = isinstance(raw_usage, dict) and (
        "cache_read_input_tokens" in raw_usage
        or "cache_creation_input_tokens" in raw_usage
    )
    merged["total_input_tokens"] = int(merged.get("input_tokens") or 0)
    if separate_cache_tokens:
        merged["total_input_tokens"] += int(merged.get("cached_tokens") or 0)
        merged["total_input_tokens"] += int(merged.get("cache_creation_tokens") or 0)
    if not isinstance(raw_usage, dict) or "total_tokens" not in raw_usage:
        merged["total_tokens"] = int(merged.get("total_input_tokens") or 0) + int(
            merged.get("output_tokens") or 0
        )

    cached_tokens = merged.get("cached_tokens")
    merged["cache_rate"] = _safe_ratio(cached_tokens, merged["total_input_tokens"])
    merged["kv_cache_hit_tokens"] = cached_tokens
    merged["kv_cache_miss_tokens"] = merged.get("cache_miss_tokens")
    merged["kv_cache_creation_tokens"] = merged.get("cache_creation_tokens")
    merged["kv_cache_hit_rate"] = merged["cache_rate"]
    return merged


def _iter_sse_events(buffer: bytes) -> tuple[list[bytes], bytes]:
    events: list[bytes] = []
    separator = b"\n\n"
    while True:
        idx = buffer.find(separator)
        if idx < 0:
            break
        events.append(buffer[:idx])
        buffer = buffer[idx + len(separator) :]
    return events, buffer


def _extract_usage_from_sse_event(event: bytes) -> dict[str, int] | None:
    payload = _decode_sse_event(event)
    return _extract_usage(payload) if isinstance(payload, dict) else None


@dataclass
class UsageSummary:
    started_at: str
    updated_at: str
    first_request_started_at: str | None
    last_request_finished_at: str | None
    total_window_seconds: float
    model_call_count: int
    input_tokens: int
    total_input_tokens: int
    output_tokens: int
    total_tokens: int
    cached_tokens: int
    cache_creation_tokens: int
    cache_miss_tokens: int
    kv_cache_hit_tokens: int
    kv_cache_creation_tokens: int
    kv_cache_miss_tokens: int


class StatsStore:
    def __init__(self, base_dir: Path, file_prefix: str = "") -> None:
        self.base_dir = base_dir
        self.token_log_path = base_dir / f"{file_prefix}token_usage_log.jsonl"
        self.message_log_path = base_dir / f"{file_prefix}message_log.jsonl"
        self.trace_log_path = base_dir / f"{file_prefix}proxy_trace_log.jsonl"
        self.summary_path = base_dir / f"{file_prefix}usage_summary.json"
        self._lock = asyncio.Lock()
        now = utc_now_iso()
        self._summary = UsageSummary(
            started_at=now,
            updated_at=now,
            first_request_started_at=None,
            last_request_finished_at=None,
            total_window_seconds=0.0,
            model_call_count=0,
            input_tokens=0,
            total_input_tokens=0,
            output_tokens=0,
            total_tokens=0,
            cached_tokens=0,
            cache_creation_tokens=0,
            cache_miss_tokens=0,
            kv_cache_hit_tokens=0,
            kv_cache_creation_tokens=0,
            kv_cache_miss_tokens=0,
        )
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._flush_summary()

    async def record(
        self,
        entry: dict[str, Any],
        usage: dict[str, Any],
        started_at: float,
        finished_at: float,
    ) -> None:
        async with self._lock:
            started_iso = datetime.fromtimestamp(
                started_at, timezone(timedelta(hours=8))
            ).isoformat()
            finished_iso = datetime.fromtimestamp(
                finished_at, timezone(timedelta(hours=8))
            ).isoformat()

            if self._summary.first_request_started_at is None:
                self._summary.first_request_started_at = started_iso
            self._summary.last_request_finished_at = finished_iso
            self._summary.total_window_seconds = max(
                0.0,
                finished_at
                - datetime.fromisoformat(
                    self._summary.first_request_started_at
                ).timestamp(),
            )
            self._summary.model_call_count += 1
            self._summary.input_tokens += int(usage["input_tokens"])
            self._summary.total_input_tokens += int(
                usage.get("total_input_tokens", usage["input_tokens"]) or 0
            )
            self._summary.output_tokens += int(usage["output_tokens"])
            self._summary.total_tokens += int(usage["total_tokens"])
            self._summary.cached_tokens += int(usage.get("cached_tokens") or 0)
            self._summary.cache_creation_tokens += int(
                usage.get("cache_creation_tokens") or 0
            )
            self._summary.cache_miss_tokens += int(usage.get("cache_miss_tokens") or 0)
            self._summary.kv_cache_hit_tokens += int(
                usage.get("kv_cache_hit_tokens") or 0
            )
            self._summary.kv_cache_creation_tokens += int(
                usage.get("kv_cache_creation_tokens") or 0
            )
            self._summary.kv_cache_miss_tokens += int(
                usage.get("kv_cache_miss_tokens") or 0
            )
            self._summary.updated_at = utc_now_iso()

            with self.token_log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry["token_log"], ensure_ascii=False) + "\n")
            with self.message_log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry["message_log"], ensure_ascii=False) + "\n")
            trace_entry = entry.get("trace_log")
            if trace_entry is not None:
                with self.trace_log_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(trace_entry, ensure_ascii=False) + "\n")
            self._flush_summary()

    def _flush_summary(self) -> None:
        with self.summary_path.open("w", encoding="utf-8") as f:
            json.dump(asdict(self._summary), f, ensure_ascii=False, indent=2)


class LocalOpenAIProxyApp:
    def __init__(
        self,
        target_base: str,
        output_dir: Path,
        timeout: float = 600.0,
        stats_file_prefix: str = "",
    ) -> None:
        self.target_base = target_base.rstrip("/")
        self.output_dir = output_dir
        self.timeout = timeout
        self.stats = StatsStore(output_dir, file_prefix=stats_file_prefix)
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=30.0))

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            await self._handle_lifespan(receive, send)
            return

        if scope["type"] != "http":
            await self._send_plain(send, 404, b"Not Found")
            return

        path = scope.get("path", "")
        if path == "/health":
            await self._send_json(send, 200, {"status": "ok"})
            return

        if not path.startswith("/v1/"):
            await self._send_plain(send, 404, b"Not Found")
            return

        await self._proxy_http(scope, receive, send)

    async def _handle_lifespan(self, receive: Any, send: Any) -> None:
        while True:
            message = await receive()
            msg_type = message["type"]
            if msg_type == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif msg_type == "lifespan.shutdown":
                await self.client.aclose()
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def _proxy_http(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        method = scope["method"]
        query_string = scope.get("query_string", b"").decode("utf-8")
        target_url = f"{self.target_base}{scope['path']}"
        if query_string:
            target_url = f"{target_url}?{query_string}"

        headers = self._prepare_headers(scope.get("headers", []))
        body = await self._read_body(receive)
        body, request_json = _prepare_request_body(
            headers.get("content-type", ""), body
        )

        request_started = time.time()
        upstream = self.client.build_request(
            method=method,
            url=target_url,
            headers=headers,
            content=body if body else None,
        )

        try:
            response = await self.client.send(upstream, stream=True)
        except httpx.HTTPError as exc:
            await self._send_json(
                send, 502, {"error": {"message": str(exc), "type": "proxy_error"}}
            )
            return

        response_headers = self._prepare_response_headers(response.headers)
        is_sse = "text/event-stream" in response.headers.get("content-type", "").lower()

        if is_sse:
            await self._stream_sse_response(
                send=send,
                response=response,
                response_headers=response_headers,
                request_started=request_started,
                method=method,
                path=scope["path"],
                query_string=query_string,
                request_json=request_json,
                request_headers=headers,
            )
            return

        raw = await response.aread()
        finished_at = time.time()
        usage = _zero_usage()
        response_json = None
        if "application/json" in response.headers.get("content-type", "").lower():
            try:
                response_json = _decode_json_bytes(raw)
                usage = _extract_usage(response_json)
            except Exception:
                response_json = None

        await send(
            {
                "type": "http.response.start",
                "status": response.status_code,
                "headers": response_headers,
            }
        )
        await send({"type": "http.response.body", "body": raw, "more_body": False})
        await response.aclose()

        await self.stats.record(
            entry=self._build_log_entry(
                method=method,
                path=scope["path"],
                query_string=query_string,
                request_json=request_json,
                response_json=response_json,
                status_code=response.status_code,
                is_stream=False,
                usage=usage,
                request_started=request_started,
                finished_at=finished_at,
                request_headers=headers,
            ),
            usage=usage,
            started_at=request_started,
            finished_at=finished_at,
        )

    async def _stream_sse_response(
        self,
        *,
        send: Any,
        response: httpx.Response,
        response_headers: list[tuple[bytes, bytes]],
        request_started: float,
        method: str,
        path: str,
        query_string: str,
        request_json: Any,
        request_headers: dict[str, str],
    ) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": response.status_code,
                "headers": response_headers,
            }
        )

        buffer = b""
        usage = _zero_usage()
        response_accumulator = StreamingResponseAccumulator()
        async for chunk in response.aiter_raw():
            if chunk:
                buffer += chunk
                events, buffer = _iter_sse_events(buffer)
                for event in events:
                    payload = _decode_sse_event(event)
                    if isinstance(payload, dict):
                        response_accumulator.add_payload(payload)
                        maybe_usage = _extract_usage(payload)
                        if maybe_usage.get("raw_usage") is not None:
                            usage = maybe_usage
            await send({"type": "http.response.body", "body": chunk, "more_body": True})

        finished_at = time.time()
        if buffer:
            payload = _decode_sse_event(buffer)
            if isinstance(payload, dict):
                response_accumulator.add_payload(payload)
                maybe_usage = _extract_usage(payload)
                if maybe_usage.get("raw_usage") is not None:
                    usage = maybe_usage

        await send({"type": "http.response.body", "body": b"", "more_body": False})
        await response.aclose()

        await self.stats.record(
            entry=self._build_log_entry(
                method=method,
                path=path,
                query_string=query_string,
                request_json=request_json,
                response_json=None,
                status_code=response.status_code,
                is_stream=True,
                usage=usage,
                request_started=request_started,
                finished_at=finished_at,
                request_headers=request_headers,
                response_messages=response_accumulator.messages(),
                stream_diagnostics=response_accumulator.diagnostics(),
            ),
            usage=usage,
            started_at=request_started,
            finished_at=finished_at,
        )

    @staticmethod
    async def _read_body(receive: Any) -> bytes:
        chunks: list[bytes] = []
        while True:
            message = await receive()
            if message["type"] != "http.request":
                continue
            if body := message.get("body", b""):
                chunks.append(body)
            if not message.get("more_body", False):
                break
        return b"".join(chunks)

    @staticmethod
    def _prepare_headers(raw_headers: list[tuple[bytes, bytes]]) -> dict[str, str]:
        headers: dict[str, str] = {}
        for key, value in raw_headers:
            key_text = key.decode("latin-1").lower()
            if key_text in {"host", "content-length"}:
                continue
            headers[key_text] = value.decode("latin-1")
        return headers

    @staticmethod
    def _prepare_response_headers(headers: httpx.Headers) -> list[tuple[bytes, bytes]]:
        excluded = {
            "content-length",
            "transfer-encoding",
            "connection",
            "content-encoding",
        }
        return [
            (key.encode("latin-1"), value.encode("latin-1"))
            for key, value in headers.items()
            if key.lower() not in excluded
        ]

    @staticmethod
    async def _send_plain(send: Any, status: int, body: bytes) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"text/plain; charset=utf-8")],
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})

    @staticmethod
    async def _send_json(send: Any, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json; charset=utf-8")],
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})

    @staticmethod
    def _extract_request_messages(request_json: Any) -> Any:
        if not isinstance(request_json, dict):
            return None
        messages = request_json.get("messages")
        if messages is not None:
            return messages
        return request_json.get("input")

    @staticmethod
    def _extract_request_tools(request_json: Any) -> Any:
        """Return the request body's top-level ``tools`` field unchanged."""
        if not isinstance(request_json, dict):
            return None
        return request_json.get("tools")

    @staticmethod
    def _extract_response_messages(response_json: Any) -> Any:
        if not isinstance(response_json, dict):
            return None
        choices = response_json.get("choices")
        if not isinstance(choices, list):
            return None

        response_messages: list[Any] = []
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            if "message" in choice:
                response_messages.append(choice["message"])
            elif "delta" in choice:
                response_messages.append(choice["delta"])
        return response_messages or None

    def _build_log_entry(
        self,
        *,
        method: str,
        path: str,
        query_string: str,
        request_json: Any,
        response_json: Any,
        status_code: int,
        is_stream: bool,
        usage: dict[str, Any],
        request_started: float,
        finished_at: float,
        request_headers: dict[str, str] | None = None,
        response_messages: Any = None,
        stream_diagnostics: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        duration = max(0.0, finished_at - request_started)
        target = f"{self.target_base}{path}"
        if query_string:
            target = f"{target}?{query_string}"
        request_started_at = datetime.fromtimestamp(
            request_started, timezone(timedelta(hours=8))
        ).isoformat()
        request_finished_at = datetime.fromtimestamp(
            finished_at, timezone(timedelta(hours=8))
        ).isoformat()
        effective_response_messages = (
            response_messages
            if response_messages is not None
            else self._extract_response_messages(response_json)
        )
        base_entry = {
            "request_started_at": request_started_at,
            "request_finished_at": request_finished_at,
            "duration_seconds": duration,
            "method": method,
            "path": path,
            "query_string": query_string,
            "target_url": target,
            "is_stream": is_stream,
            "status_code": status_code,
            "model": request_json.get("model")
            if isinstance(request_json, dict)
            else None,
        }
        return {
            "token_log": {
                **base_entry,
                **usage,
            },
            "message_log": {
                **base_entry,
                "request_messages": self._extract_request_messages(request_json),
                "request_tools": self._extract_request_tools(request_json),
                "response_messages": effective_response_messages,
            },
            "trace_log": {
                **base_entry,
                "proxy_runtime": _proxy_runtime_snapshot(),
                "request": {
                    "headers": _safe_trace_headers(request_headers or {}),
                    "top_level_keys": (
                        sorted(str(key) for key in request_json)
                        if isinstance(request_json, dict)
                        else []
                    ),
                    "context_fields": _extract_context_fields(request_json),
                    "workspace_mentions": _extract_workspace_mentions(request_json),
                    "tool_calls": _summarize_tool_calls(
                        self._extract_request_messages(request_json)
                    ),
                },
                "response": {
                    "tool_calls": _summarize_tool_calls(effective_response_messages),
                    **(stream_diagnostics or {}),
                },
            },
        }


def create_app(
    *, target_base: str | None = None, output_dir: str | Path | None = None
) -> LocalOpenAIProxyApp:
    base_dir = (
        Path(output_dir)
        if output_dir
        else Path(
            "C:/yzj/files/open_source/jiuwenclaw_4.21/jiuwenclaw/local_api_server"
        )
    )
    return LocalOpenAIProxyApp(
        target_base=target_base
        or os.getenv("LOCAL_OPENAI_PROXY_TARGET_BASE", "https://api.deepseek.com"),
        output_dir=base_dir,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Local OpenAI-compatible transparent proxy server"
    )
    parser.add_argument(
        "--host", default=os.getenv("LOCAL_OPENAI_PROXY_HOST", "127.0.0.1")
    )
    parser.add_argument(
        "--port", type=int, default=int(os.getenv("LOCAL_OPENAI_PROXY_PORT", "9527"))
    )
    parser.add_argument(
        "--target-base",
        default=os.getenv(
            "LOCAL_OPENAI_PROXY_TARGET_BASE", "https://api.deepseek.com"
        ),
    )
    parser.add_argument(
        "--output-dir",
        default="C:/yzj/files/open_source/jiuwenclaw_4.21/jiuwenclaw/local_api_server",
    )
    parser.add_argument(
        "--log-level", default=os.getenv("LOCAL_OPENAI_PROXY_LOG_LEVEL", "info")
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    app = create_app(target_base=args.target_base, output_dir=args.output_dir)
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
