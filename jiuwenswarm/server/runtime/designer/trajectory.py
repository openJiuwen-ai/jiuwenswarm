# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Per-project trajectory files for Designer runs.

Two append-only JSONL files live under ``<data dir>/.trace/designer/``:

* ``<key>.otlp.jsonl`` — one OTLP/JSON ``resourceSpans`` envelope per line, the
  same shape the session trajectory store keeps in ``raw_json`` (prompts, tool
  arguments/results, timings).
* ``<key>.design.jsonl`` — Designer-only records: run metadata, director and
  collaboration events, feedback and per-agent run summaries.

Nothing is written unless ``trajectory_ui.enabled`` is on.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jiuwenswarm.common.schema.designer_graph import utc_now_ms
from jiuwenswarm.observability.config import (
    TrajectoryStoreSettings,
    load_trajectory_store_settings,
)
from jiuwenswarm.server.runtime.designer.paths import (
    design_otlp_trajectory_path,
    design_record_trajectory_path,
    design_trajectory_dir,
    design_trajectory_key,
)

logger = logging.getLogger(__name__)

TRAJECTORY_SCHEMA = "designer-trajectory.v3"
OTLP_SERVICE_NAME = "jiuwenswarm-designer"
OTLP_SCOPE_NAME = "jiuwenswarm.server.runtime.designer"
_OTLP_STATUS_OK = 1
_OTLP_STATUS_ERROR = 2
_OTLP_SPAN_KIND_INTERNAL = 1
_OTLP_SPAN_KIND_CLIENT = 3

_lock = threading.RLock()
_active: dict[str, TrajectoryRecorder] = {}


@dataclass(frozen=True)
class TrajectoryScope:
    """Agent identity inherited by nested model and tool calls."""

    recorder: TrajectoryRecorder
    agent_id: str
    role: str
    phase: str
    action: str
    span_id: str


_current_scope: ContextVar[TrajectoryScope | None] = ContextVar(
    "designer_trajectory_scope",
    default=None,
)


def _json_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _otlp_value(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
        return {"arrayValue": {"values": [{"stringValue": item} for item in value]}}
    return {"stringValue": _json_text(value)}


def _otlp_attributes(attributes: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"key": key, "value": _otlp_value(value)}
        for key, value in attributes.items()
        if value is not None and value != ""
    ]


def _message_parts(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "content": content}]
    if not isinstance(content, list):
        return [{"type": "text", "content": _json_text(content)}]
    parts: list[dict[str, Any]] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append({"type": "text", "content": str(item.get("text") or "")})
        elif isinstance(item, dict) and item.get("type") == "image_url":
            image = item.get("image_url")
            uri = image.get("url") if isinstance(image, dict) else image
            parts.append({"type": "uri", "modality": "image", "uri": str(uri or "")})
        else:
            parts.append({"type": "text", "content": _json_text(item)})
    return parts


def _gen_ai_messages(messages: list[Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split OpenAI-style messages into GenAI system instructions and input messages."""
    system: list[dict[str, Any]] = []
    converted: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            converted.append({"role": "user", "parts": _message_parts(message)})
            continue
        role = str(message.get("role") or "user")
        parts = _message_parts(message.get("content"))
        if role == "system":
            system.extend(parts)
        else:
            converted.append({"role": role, "parts": parts})
    return system, converted


def _agent_call_attributes(detail: dict[str, Any]) -> tuple[str, int, dict[str, Any]]:
    agent_type = str(detail.pop("agent_type", "") or "")
    operation = "invoke_agent" if agent_type == "deep_agent" else "chat"
    attributes: dict[str, Any] = {"gen_ai.operation.name": operation}
    prompt = detail.pop("prompt", None)
    system_prompt = detail.pop("system_prompt", None)
    request = detail.pop("input", None)
    request = dict(request) if isinstance(request, dict) else {}

    system_parts: list[dict[str, Any]] = []
    input_messages: list[dict[str, Any]] = []
    messages = request.pop("messages", None)
    if isinstance(messages, list):
        system_parts, input_messages = _gen_ai_messages(messages)
    elif prompt:
        input_messages = [{"role": "user", "parts": _message_parts(prompt)}]
    if system_prompt:
        system_parts = [{"type": "text", "content": str(system_prompt)}]
    if system_parts:
        attributes["gen_ai.system_instructions"] = system_parts
    if input_messages:
        attributes["gen_ai.input.messages"] = input_messages

    model = request.pop("model", None)
    attributes["gen_ai.request.model"] = model
    attributes["gen_ai.request.max_tokens"] = request.pop("max_tokens", None)
    attributes["gen_ai.request.temperature"] = request.pop("temperature", None)
    if agent_type == "deep_agent":
        request.pop("query", None)
    if request:
        detail["request"] = request

    output = detail.pop("output", None)
    finish_reason = detail.pop("finish_reason", None)
    if output is not None:
        message: dict[str, Any] = {"role": "assistant", "parts": _message_parts(output)}
        if finish_reason:
            message["finish_reason"] = finish_reason
        attributes["gen_ai.output.messages"] = [message]
    if finish_reason:
        attributes["gen_ai.response.finish_reasons"] = [str(finish_reason)]
    attributes["gen_ai.response.model"] = detail.pop("response_model", None)
    usage = detail.pop("usage", None)
    if isinstance(usage, dict):
        attributes["gen_ai.usage.input_tokens"] = usage.get("input_tokens")
        attributes["gen_ai.usage.output_tokens"] = usage.get("output_tokens")

    if operation == "chat":
        name = f"chat {model}" if model else "chat"
        return name, _OTLP_SPAN_KIND_CLIENT, attributes
    return "invoke_agent", _OTLP_SPAN_KIND_INTERNAL, attributes


def _tool_call_attributes(tool: str | None, detail: dict[str, Any]) -> tuple[str, int, dict[str, Any]]:
    attributes: dict[str, Any] = {
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.tool.name": tool,
    }
    if "input" in detail:
        attributes["gen_ai.tool.call.arguments"] = _json_text(detail.pop("input"))
    if "output" in detail:
        attributes["gen_ai.tool.call.result"] = _json_text(detail.pop("output"))
    return f"execute_tool {tool}" if tool else "execute_tool", _OTLP_SPAN_KIND_INTERNAL, attributes


class _JsonlFile:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._failed = False

    def append(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, default=str)
        with _lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError as exc:
                if not self._failed:
                    logger.warning("Designer trajectory write failed for %s: %s", self.path, exc)
                self._failed = True


class TrajectoryRecorder:
    """Streams one Designer run into its project's OTLP and design files.

    A recorder without paths is disabled: spans and records become no-ops.
    """

    def __init__(
        self,
        graph_id: str,
        run_id: str,
        *,
        project_id: str,
        key: str = "",
        otlp_path: Path | None = None,
        design_path: Path | None = None,
    ) -> None:
        self.graph_id = graph_id
        self.run_id = run_id
        self.project_id = project_id
        self.key = key
        self.trace_id = secrets.token_hex(16)
        self.started_at_ms = utc_now_ms()
        self.agents: dict[str, dict[str, Any]] = {}
        self._otlp = _JsonlFile(otlp_path) if otlp_path is not None else None
        self._design = _JsonlFile(design_path) if design_path is not None else None

    @property
    def enabled(self) -> bool:
        return self._otlp is not None and self._design is not None

    @property
    def otlp_path(self) -> Path | None:
        return self._otlp.path if self._otlp is not None else None

    @property
    def design_path(self) -> Path | None:
        return self._design.path if self._design is not None else None

    def _count(self, agent_id: str, role: str, action: str, duration_ms: float) -> None:
        with _lock:
            bucket = self.agents.setdefault(
                agent_id,
                {"agent_id": agent_id, "role": role, "total_ms": 0.0, "tool_calls": 0, "agent_calls": 0},
            )
            if role and not bucket["role"]:
                bucket["role"] = role
            bucket["total_ms"] += duration_ms
            if action == "tool_call":
                bucket["tool_calls"] += 1
            elif action == "agent_call":
                bucket["agent_calls"] += 1

    def _write_design(self, kind: str, body: dict[str, Any]) -> None:
        if self._design is None:
            return
        self._design.append(
            {
                "schema_version": TRAJECTORY_SCHEMA,
                "kind": kind,
                "ts_ms": utc_now_ms(),
                "project_id": self.project_id,
                "graph_id": self.graph_id,
                "run_id": self.run_id,
                "trace_id": self.trace_id,
                **body,
            }
        )

    def _write_span(
        self,
        *,
        scope: TrajectoryScope,
        parent_span_id: str | None,
        tool: str | None,
        detail: dict[str, Any],
        start_ns: int,
        end_ns: int,
        error: str | None,
    ) -> None:
        if self._otlp is None:
            return
        remaining = deepcopy(detail)
        if scope.action == "agent_call":
            name, kind, attributes = _agent_call_attributes(remaining)
        elif scope.action == "tool_call":
            name, kind, attributes = _tool_call_attributes(tool, remaining)
        else:
            name, kind, attributes = f"{scope.agent_id}.{scope.action}", _OTLP_SPAN_KIND_INTERNAL, {}
        attributes.update(
            {
                "session.id": self.key,
                "gen_ai.conversation.id": self.key,
                "gen_ai.agent.id": scope.agent_id,
                "gen_ai.agent.name": scope.role or scope.agent_id,
                "openjiuwen.run.id": self.run_id,
                "designer.project.id": self.project_id,
                "designer.graph.id": self.graph_id,
                "designer.phase": scope.phase,
                "designer.action": scope.action,
                "designer.tool": tool,
                "error.type": error.split(":", 1)[0] if error else None,
            }
        )
        if remaining:
            attributes["designer.detail"] = remaining
        span: dict[str, Any] = {
            "traceId": self.trace_id,
            "spanId": scope.span_id,
            "name": name,
            "kind": kind,
            "startTimeUnixNano": str(start_ns),
            "endTimeUnixNano": str(end_ns),
            "attributes": _otlp_attributes(attributes),
            "status": (
                {"code": _OTLP_STATUS_ERROR, "message": error}
                if error
                else {"code": _OTLP_STATUS_OK}
            ),
        }
        if parent_span_id:
            span["parentSpanId"] = parent_span_id
        self._otlp.append(
            {
                "resourceSpans": [
                    {
                        "resource": {
                            "attributes": _otlp_attributes({"service.name": OTLP_SERVICE_NAME})
                        },
                        "scopeSpans": [{"scope": {"name": OTLP_SCOPE_NAME}, "spans": [span]}],
                    }
                ]
            }
        )

    def start(self, meta: dict[str, Any] | None = None) -> None:
        self._write_design(
            "run_started",
            {"started_at_ms": self.started_at_ms, "meta": deepcopy(meta or {})},
        )

    def record(
        self,
        *,
        agent_id: str,
        action: str,
        phase: str = "work",
        role: str = "",
        tool: str | None = None,
        detail: dict[str, Any] | None = None,
        status: str = "ok",
    ) -> None:
        if not self.enabled:
            return
        scope = _current_scope.get()
        self._write_design(
            "event",
            {
                "span_id": scope.span_id if scope is not None and scope.recorder is self else None,
                "agent_id": agent_id,
                "agent_role": role,
                "phase": phase,
                "action": action,
                "tool": tool,
                "status": status,
                "detail": deepcopy(detail or {}),
            },
        )

    @contextmanager
    def span(
        self,
        *,
        agent_id: str,
        action: str,
        phase: str = "work",
        role: str = "",
        tool: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        payload: dict[str, Any] = deepcopy(detail or {})
        if not self.enabled:
            yield payload
            return
        parent = _current_scope.get()
        parent_span_id = parent.span_id if parent is not None and parent.recorder is self else None
        scope = TrajectoryScope(
            recorder=self,
            agent_id=agent_id,
            role=role,
            phase=phase,
            action=action,
            span_id=secrets.token_hex(8),
        )
        error: str | None = None
        start_ns = time.time_ns()
        token = _current_scope.set(scope)
        try:
            yield payload
        except BaseException as exc:
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            _current_scope.reset(token)
            end_ns = time.time_ns()
            self._write_span(
                scope=scope,
                parent_span_id=parent_span_id,
                tool=tool,
                detail=payload,
                start_ns=start_ns,
                end_ns=end_ns,
                error=error,
            )
            self._count(agent_id, role, action, (end_ns - start_ns) / 1_000_000)

    def set_feedback(self, feedback: dict[str, Any]) -> None:
        self._write_design("feedback", {"feedback": deepcopy(feedback)})

    def finish(self) -> None:
        with _lock:
            agents = deepcopy(self.agents)
        self._write_design(
            "run_ended",
            {
                "started_at_ms": self.started_at_ms,
                "ended_at_ms": utc_now_ms(),
                "agents": agents,
            },
        )


def _prune_expired(directory: Path, retention_days: int) -> None:
    cutoff = time.time() - retention_days * 86400
    try:
        candidates = list(directory.glob("*.jsonl"))
    except OSError:
        return
    for path in candidates:
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            logger.debug("Designer trajectory prune failed for %s", path, exc_info=True)


def begin_trajectory(
    graph_id: str,
    run_id: str,
    *,
    project_id: str,
    meta: dict[str, Any] | None = None,
    settings: TrajectoryStoreSettings | None = None,
) -> TrajectoryRecorder:
    resolved = settings or load_trajectory_store_settings()
    if not resolved.enabled:
        return TrajectoryRecorder(graph_id, run_id, project_id=project_id)
    key = design_trajectory_key(project_id)
    _prune_expired(design_trajectory_dir(), resolved.retention_days)
    rec = TrajectoryRecorder(
        graph_id,
        run_id,
        project_id=project_id,
        key=key,
        otlp_path=design_otlp_trajectory_path(key),
        design_path=design_record_trajectory_path(key),
    )
    rec.start(meta)
    with _lock:
        _active[run_id] = rec
    return rec


def get_trajectory(run_id: str) -> TrajectoryRecorder | None:
    with _lock:
        return _active.get(run_id)


def get_current_trajectory_scope() -> TrajectoryScope | None:
    """Return the active agent scope for this async execution context."""
    return _current_scope.get()


@contextmanager
def current_trajectory_span(
    *,
    action: str,
    tool: str | None = None,
    detail: dict[str, Any] | None = None,
    phase: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Record a nested call when executing inside a Designer trajectory span."""
    scope = get_current_trajectory_scope()
    if scope is None:
        yield deepcopy(detail or {})
        return
    nested_detail = deepcopy(detail or {})
    nested_detail.setdefault("parent_action", scope.action)
    with scope.recorder.span(
        agent_id=scope.agent_id,
        action=action,
        phase=phase or scope.phase,
        role=scope.role,
        tool=tool,
        detail=nested_detail,
    ) as payload:
        yield payload


def end_trajectory(run_id: str) -> str | None:
    with _lock:
        rec = _active.pop(run_id, None)
    if rec is None:
        return None
    rec.finish()
    return str(rec.design_path) if rec.design_path is not None else None
