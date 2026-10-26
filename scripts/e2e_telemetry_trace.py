#!/usr/bin/env python3
"""Drive a live Gateway WebSocket flow and seal fresh OTLP JSON evidence."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import sys
import time
from typing import Any, Iterable
from uuid import uuid4

import websockets


_LOGGER = logging.getLogger(__name__)
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EVIDENCE_ROOT = REPO_ROOT / ".telemetry-evidence"
DEFAULT_COLLECTOR_EVIDENCE_DIR = Path(
    os.getenv("TELEMETRY_EVIDENCE_DIR") or DEFAULT_EVIDENCE_ROOT
)
DEFAULT_EVIDENCE = Path(
    os.getenv("TELEMETRY_E2E_EVIDENCE")
    or DEFAULT_EVIDENCE_ROOT / "evidence.json"
)
DEFAULT_TRACE_JSON = DEFAULT_COLLECTOR_EVIDENCE_DIR / "traces.jsonl"
DEFAULT_METRIC_JSON = DEFAULT_COLLECTOR_EVIDENCE_DIR / "metrics.jsonl"
SCHEMA_VERSION = 1
EXPECTED_METRICS = {
    "jiuwenclaw.request.duration",
    "jiuwenclaw.request.count",
    "jiuwenclaw.request.error.count",
    "jiuwenclaw.agent.duration",
    "gen_ai.client.operation.duration",
    "gen_ai.client.operation.count",
    "gen_ai.client.token.usage",
    "gen_ai.tool.duration",
    "gen_ai.tool.call.count",
    "gen_ai.tool.error.count",
    "jiuwenclaw.session.active",
    "jiuwenclaw.session.created.count",
    "jiuwenclaw.session.state",
    "jiuwenclaw.session.stuck",
    "jiuwenclaw.session.stuck_age_ms",
    "gen_ai.skill.call.count",
    "gen_ai.skill.duration",
    "gen_ai.skill.error.count",
    "gen_ai.tool.token.usage",
    "gen_ai.skill.token.usage",
    "gen_ai.client.token.first_token_duration",
}
FORBIDDEN_SPAN_NAMES = {
    "jiuwenswarm.agent.invoke",
    "jiuwenswarm.agent.invoke.stream",
    "gen_ai.chat",
}
FORBIDDEN_SPAN_PREFIXES = ("gen_ai.tool.execute:",)


class EvidenceError(RuntimeError):
    """Raised when runtime evidence is missing, stale, or structurally invalid."""


def _now_ns() -> int:
    return time.time_ns()


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return 0


def _otel_value(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    for key in ("stringValue", "boolValue", "bytesValue"):
        if key in value:
            return value[key]
    if "intValue" in value:
        return _int(value["intValue"])
    if "doubleValue" in value:
        try:
            return float(value["doubleValue"])
        except (TypeError, ValueError, OverflowError):
            return 0.0
    array = value.get("arrayValue")
    if isinstance(array, dict):
        return [_otel_value(item) for item in array.get("values", [])]
    key_values = value.get("kvlistValue")
    if isinstance(key_values, dict):
        return _attributes(key_values.get("values", []))
    return value


def _attributes(items: Any) -> dict[str, Any]:
    if isinstance(items, dict):
        return dict(items)
    if not isinstance(items, list):
        return {}
    return {
        str(item.get("key")): _otel_value(item.get("value"))
        for item in items
        if isinstance(item, dict) and item.get("key")
    }


def read_json_records(path: Path) -> list[dict[str, Any]]:
    """Read either one JSON document or append-only JSON-lines batches."""
    if not path.is_file():
        raise EvidenceError(f"collector evidence file not found: {path}")
    text = path.read_text(encoding="utf-8", errors="strict").strip()
    if not text:
        return []
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        records: list[dict[str, Any]] = []
        for line_number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise EvidenceError(
                    f"invalid collector JSON at {path}:{line_number}: {error}"
                ) from error
            if isinstance(value, dict):
                records.append(value)
        return records
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return [value] if isinstance(value, dict) else []


def extract_spans(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    for record in records:
        for resource_spans in record.get("resourceSpans", []):
            resource = _attributes(
                (resource_spans.get("resource") or {}).get("attributes", [])
            )
            for scope_spans in resource_spans.get("scopeSpans", []):
                for span in scope_spans.get("spans", []):
                    if not isinstance(span, dict):
                        continue
                    spans.append(
                        {
                            "trace_id": str(span.get("traceId") or ""),
                            "span_id": str(span.get("spanId") or ""),
                            "parent_span_id": str(span.get("parentSpanId") or ""),
                            "name": str(span.get("name") or ""),
                            "start_unix_nano": _int(span.get("startTimeUnixNano")),
                            "end_unix_nano": _int(span.get("endTimeUnixNano")),
                            "attributes": _attributes(span.get("attributes", [])),
                            "resource": resource,
                        }
                    )
    return spans


def _metric_data_points(metric: dict[str, Any]) -> Iterable[dict[str, Any]]:
    for data_key in (
        "gauge",
        "sum",
        "histogram",
        "exponentialHistogram",
        "summary",
    ):
        data = metric.get(data_key)
        if isinstance(data, dict):
            yield from (
                point for point in data.get("dataPoints", []) if isinstance(point, dict)
            )


def extract_metric_points(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    for record in records:
        for resource_metrics in record.get("resourceMetrics", []):
            resource = _attributes(
                (resource_metrics.get("resource") or {}).get("attributes", [])
            )
            for scope_metrics in resource_metrics.get("scopeMetrics", []):
                for metric in scope_metrics.get("metrics", []):
                    if not isinstance(metric, dict):
                        continue
                    for point in _metric_data_points(metric):
                        points.append(
                            {
                                "name": str(metric.get("name") or ""),
                                "time_unix_nano": _int(
                                    point.get("timeUnixNano")
                                    or point.get("startTimeUnixNano")
                                ),
                                "attributes": _attributes(point.get("attributes", [])),
                                "resource": resource,
                            }
                        )
    return points


def load_fresh_evidence(
    path: Path,
    *,
    max_age_seconds: float = 900.0,
    now_ns: int | None = None,
) -> dict[str, Any]:
    """Load evidence and reject reports not backed by this live run window."""
    try:
        evidence = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise EvidenceError(
            f"fresh evidence missing: {path}; run scripts/e2e_telemetry_trace.py"
        ) from error
    except json.JSONDecodeError as error:
        raise EvidenceError(f"invalid evidence JSON: {path}: {error}") from error
    if (
        not isinstance(evidence, dict)
        or evidence.get("schema_version") != SCHEMA_VERSION
    ):
        raise EvidenceError("unsupported telemetry evidence schema")
    source = evidence.get("source") or {}
    if source.get("kind") != "live-otel-collector":
        raise EvidenceError("evidence source must be live-otel-collector")
    started = _int(evidence.get("run_started_unix_nano"))
    generated = _int(evidence.get("generated_at_unix_nano"))
    current = _now_ns() if now_ns is None else now_ns
    if started <= 0 or generated < started or generated > current + 5_000_000_000:
        raise EvidenceError("invalid telemetry evidence time window")
    if current - generated > int(max_age_seconds * 1_000_000_000):
        raise EvidenceError("telemetry evidence is stale; drive a new run")
    flows = evidence.get("flows")
    if not isinstance(flows, list) or not flows:
        raise EvidenceError("telemetry evidence contains no live WebSocket flows")
    return evidence


def _dedupe(
    items: Iterable[dict[str, Any]], keys: tuple[str, ...]
) -> list[dict[str, Any]]:
    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for item in items:
        identity = tuple(
            json.dumps(item.get(key), ensure_ascii=False, sort_keys=True)
            if isinstance(item.get(key), (dict, list))
            else item.get(key)
            for key in keys
        )
        unique[identity] = item
    return list(unique.values())


def _load_append_target(
    path: Path, started_ns: int, *, new_run: bool
) -> dict[str, Any]:
    if not new_run and path.is_file():
        try:
            existing = load_fresh_evidence(path, max_age_seconds=900)
        except EvidenceError:
            existing = None
        if existing is not None:
            return existing
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": uuid4().hex,
        "run_started_unix_nano": started_ns,
        "generated_at_unix_nano": started_ns,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": {"kind": "live-otel-collector"},
        "flows": [],
        "spans": [],
        "metrics": [],
    }


async def drive_websocket_flow(
    url: str,
    *,
    mode: str,
    streaming: bool,
    scenario: str,
    request_id: str,
    session_id: str,
    timeout: float,
) -> dict[str, Any]:
    """Send a real chat request and wait for a terminal Gateway frame."""
    params = {
        "session_id": session_id,
        "content": f"telemetry e2e {mode} {scenario} {request_id}",
        "mode": mode,
        "is_stream": streaming,
    }
    request = {"type": "req", "id": request_id, "method": "chat.send", "params": params}
    cancel_request_id = f"{request_id}-cancel"
    accepted = False
    cancel_accepted = False
    terminal = ""
    frames: list[dict[str, Any]] = []
    async with websockets.connect(url, max_size=10_000_000) as ws:
        await ws.send(json.dumps(request, ensure_ascii=False))
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            remaining = max(0.1, deadline - asyncio.get_running_loop().time())
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            frame = json.loads(raw)
            if not isinstance(frame, dict):
                continue
            frames.append(frame)
            if frame.get("type") == "res" and frame.get("id") == request_id:
                if frame.get("ok") is not True:
                    raise EvidenceError(f"Gateway rejected chat.send: {frame}")
                accepted = True
                if scenario == "cancel":
                    # A cold per-session agent build can span two keepalive
                    # periods. Wait until that active run reaches the model so
                    # cancellation covers the real root and child spans.
                    await asyncio.sleep(22.0)
                    await ws.send(
                        json.dumps(
                            {
                                "type": "req",
                                "id": cancel_request_id,
                                "method": "chat.interrupt",
                                "params": {
                                    "session_id": session_id,
                                    "intent": "cancel",
                                },
                            },
                            ensure_ascii=False,
                        )
                    )
                continue
            if frame.get("type") == "res" and frame.get("id") == cancel_request_id:
                if frame.get("ok") is not True:
                    raise EvidenceError(f"Gateway rejected chat.interrupt: {frame}")
                cancel_accepted = True
                continue
            if frame.get("type") != "event":
                continue
            event = str(frame.get("event") or "")
            payload = frame.get("payload") or {}
            if scenario == "cancel" and event == "chat.interrupt_result":
                # The interrupt event can race ahead of the request response;
                # a successful runtime result is the stronger acceptance.
                cancel_accepted = bool(payload.get("success", True))
                terminal = event
                break
            if event == "chat.error":
                terminal = event
                break
            if scenario != "cancel":
                if event == "chat.final" or (
                    event == "chat.processing_status" and payload.get("is_complete")
                ):
                    terminal = event
                    break
    if not accepted:
        raise EvidenceError(f"Gateway never accepted request {request_id}")
    if scenario == "cancel" and not cancel_accepted:
        raise EvidenceError(f"Gateway never accepted cancellation for {request_id}")
    if not terminal:
        raise EvidenceError(f"Gateway emitted no terminal event for {request_id}")
    return {"terminal_event": terminal, "frame_count": len(frames)}


async def _handle_mock_llm(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    """Serve the minimal OpenAI chat-completions contract used by live E2E."""
    try:
        header = await reader.readuntil(b"\r\n\r\n")
        content_length = 0
        for line in header.decode("latin-1").split("\r\n"):
            if line.lower().startswith("content-length:"):
                content_length = int(line.split(":", 1)[1].strip())
        body = await reader.readexactly(content_length) if content_length else b"{}"
        request = json.loads(body)
        model = str(request.get("model") or "mock-model")
        request_text = body.decode("utf-8", errors="replace")
        cancel_probe = any(
            f"telemetry e2e {mode} cancel" in request_text
            for mode in ("code.normal", "agent.plan", "team")
        )
        stuck_probe = any(
            f"telemetry e2e {mode} stuck" in request_text
            for mode in ("code.normal", "agent.plan", "team")
        )
        tool_error_probe = "telemetry e2e agent.plan tool_error" in request_text
        if cancel_probe:
            await asyncio.sleep(60.0)
        elif stuck_probe:
            await asyncio.sleep(1.0)
        wants_skill_tool = "telemetry e2e agent.plan" in request_text and not any(
            isinstance(message, dict) and message.get("role") == "tool"
            for message in request.get("messages", [])
        )
        requested_skill = (
            "telemetry-missing-skill" if tool_error_probe else "skill-creator"
        )
        if request.get("stream"):
            if wants_skill_tool:
                chunks = [
                    {
                        "id": "chatcmpl-telemetry-tool",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "role": "assistant",
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "call-telemetry-skill",
                                            "type": "function",
                                            "function": {
                                                "name": "skill_tool",
                                                "arguments": json.dumps(
                                                    {
                                                        "skill_name": requested_skill,
                                                        "relative_file_path": "SKILL.md",
                                                    },
                                                    separators=(",", ":"),
                                                ),
                                            },
                                        }
                                    ],
                                },
                            }
                        ],
                    },
                    {
                        "id": "chatcmpl-telemetry-tool",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {},
                                "finish_reason": "tool_calls",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 8,
                            "completion_tokens": 2,
                            "total_tokens": 10,
                        },
                    },
                ]
            else:
                chunks = [
                    {
                        "id": "chatcmpl-telemetry",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "role": "assistant",
                                    "content": "telemetry",
                                },
                            }
                        ],
                    },
                    {
                        "id": "chatcmpl-telemetry",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": model,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        "usage": {
                            "prompt_tokens": 8,
                            "completion_tokens": 2,
                            "total_tokens": 10,
                        },
                    },
                ]
            payload = (
                "".join(
                    f"data: {json.dumps(chunk, separators=(',', ':'))}\n\n"
                    for chunk in chunks
                )
                + "data: [DONE]\n\n"
            )
            response = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: text/event-stream\r\n"
                "Cache-Control: no-cache\r\n"
                "Connection: close\r\n"
                f"Content-Length: {len(payload.encode())}\r\n\r\n"
                f"{payload}"
            )
        elif wants_skill_tool:
            payload = json.dumps(
                {
                    "id": "chatcmpl-telemetry-tool",
                    "object": "chat.completion",
                    "created": 1,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-telemetry-skill",
                                        "type": "function",
                                        "function": {
                                            "name": "skill_tool",
                                            "arguments": json.dumps(
                                                {
                                                    "skill_name": requested_skill,
                                                    "relative_file_path": "SKILL.md",
                                                },
                                                separators=(",", ":"),
                                            ),
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 8,
                        "completion_tokens": 2,
                        "total_tokens": 10,
                    },
                },
                separators=(",", ":"),
            )
            response = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: application/json\r\n"
                "Connection: close\r\n"
                f"Content-Length: {len(payload.encode())}\r\n\r\n"
                f"{payload}"
            )
        else:
            payload = json.dumps(
                {
                    "id": "chatcmpl-telemetry",
                    "object": "chat.completion",
                    "created": 1,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "telemetry"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 8,
                        "completion_tokens": 2,
                        "total_tokens": 10,
                    },
                },
                separators=(",", ":"),
            )
            response = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: application/json\r\n"
                "Connection: close\r\n"
                f"Content-Length: {len(payload.encode())}\r\n\r\n"
                f"{payload}"
            )
        writer.write(response.encode())
        await writer.drain()
    except (asyncio.IncompleteReadError, ValueError):
        try:
            writer.write(
                b"HTTP/1.1 400 Bad Request\r\n"
                b"Content-Length: 0\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (BrokenPipeError, ConnectionResetError):
            pass


def _span_matches_request(span: dict[str, Any], request_id: str) -> bool:
    attributes = span.get("attributes") or {}
    return attributes.get("jiuwenclaw.request.id") == request_id


async def _wait_for_request_trace(
    path: Path,
    request_id: str,
    *,
    mode: str,
    scenario: str,
    entrypoint: str,
    timeout: float,
) -> list[dict[str, Any]]:
    deadline = asyncio.get_running_loop().time() + timeout
    last_error: Exception | None = None
    missing = "request trace"
    while asyncio.get_running_loop().time() < deadline:
        try:
            spans = extract_spans(read_json_records(path))
            matches = [
                span for span in spans if _span_matches_request(span, request_id)
            ]
            if matches:
                trace_ids = {span["trace_id"] for span in matches}
                trace_spans = [span for span in spans if span["trace_id"] in trace_ids]
                names = {span["name"] for span in trace_spans}
                has_gateway = entrypoint != "gateway" or "channel.request" in names
                has_root = any(
                    span["name"].startswith("team.")
                    if mode == "team"
                    else span["name"].startswith("agent.")
                    and (span.get("attributes") or {}).get("jiuwenswarm.mode")
                    in ({"agent", mode} if mode == "agent.plan" else {mode})
                    for span in trace_spans
                )
                needs_llm = scenario in {"success", "stuck", "tool_error"}
                has_llm = not needs_llm or "llm.call" in names
                if has_gateway and has_root and has_llm:
                    # Let a batch exporter append siblings that ended in the
                    # same interval before sealing the consolidated evidence.
                    await asyncio.sleep(1.0)
                    spans = extract_spans(read_json_records(path))
                    return [span for span in spans if span["trace_id"] in trace_ids]
                missing_labels = []
                for label, present in (
                    ("channel.request", has_gateway),
                    (f"{mode} root", has_root),
                    ("llm.call", has_llm),
                ):
                    if not present:
                        missing_labels.append(label)
                missing = ", ".join(missing_labels)
        except (EvidenceError, OSError) as error:
            last_error = error
        await asyncio.sleep(0.5)
    raise EvidenceError(
        "collector trace incomplete for "
        f"request_id={request_id}; missing {missing}; last_error={last_error}"
    )


async def _wait_for_metric_points(
    path: Path,
    *,
    run_started: int,
    required_names: set[str],
    timeout: float,
) -> list[dict[str, Any]]:
    deadline = asyncio.get_running_loop().time() + timeout
    last_error: Exception | None = None
    missing = set(required_names)
    while asyncio.get_running_loop().time() < deadline:
        try:
            points = [
                point
                for point in extract_metric_points(read_json_records(path))
                if point["time_unix_nano"] >= run_started
            ]
            missing = required_names - {point["name"] for point in points}
            if not missing:
                return points
        except (EvidenceError, OSError) as error:
            last_error = error
        await asyncio.sleep(0.5)
    raise EvidenceError(
        "collector metrics incomplete; missing "
        f"{', '.join(sorted(missing))}; last_error={last_error}"
    )


async def run(args: argparse.Namespace) -> Path:
    invocation_started = _now_ns()
    evidence_path = args.evidence.resolve()
    evidence = _load_append_target(
        evidence_path,
        invocation_started,
        new_run=args.new_run,
    )
    request_id = args.request_id or f"telemetry-{uuid4().hex}"
    session_id = args.session_id or f"telemetry-session-{uuid4().hex[:12]}"
    channel_id = args.channel_id or (
        "tui" if args.gateway_url.rstrip("/").endswith("/tui") else "web"
    )
    mock_server = None
    if args.mock_llm_port:
        mock_server = await asyncio.start_server(
            _handle_mock_llm,
            "127.0.0.1",
            args.mock_llm_port,
        )
    try:
        if args.ingest_only:
            outcome = {"terminal_event": "externally-driven", "frame_count": 0}
        else:
            outcome = await drive_websocket_flow(
                args.gateway_url,
                mode=args.mode,
                streaming=args.streaming,
                scenario=args.scenario,
                request_id=request_id,
                session_id=session_id,
                timeout=args.timeout,
            )
    finally:
        if mock_server is not None:
            mock_server.close()
            await mock_server.wait_closed()
    trace_spans = await _wait_for_request_trace(
        args.trace_json.resolve(),
        request_id,
        mode=args.mode,
        scenario=args.scenario,
        entrypoint=args.entrypoint,
        timeout=args.collector_timeout,
    )
    required_metrics = {
        "jiuwenclaw.request.duration",
        "jiuwenclaw.request.count",
    }
    if args.scenario in {"success", "stuck", "tool_error"}:
        required_metrics.update(
            {
                "jiuwenclaw.agent.duration",
                "gen_ai.client.operation.duration",
                "gen_ai.client.operation.count",
                "gen_ai.client.token.usage",
            }
        )
    if args.streaming and args.scenario != "cancel":
        required_metrics.add("gen_ai.client.token.first_token_duration")
    if args.scenario == "cancel":
        required_metrics.add("jiuwenclaw.request.error.count")
    if args.scenario == "stuck":
        required_metrics.update(
            {"jiuwenclaw.session.stuck", "jiuwenclaw.session.stuck_age_ms"}
        )
    if args.scenario == "tool_error":
        required_metrics.update({"gen_ai.tool.error.count", "gen_ai.skill.error.count"})
    metric_points = await _wait_for_metric_points(
        args.metric_json.resolve(),
        run_started=invocation_started,
        required_names=required_metrics,
        timeout=args.collector_timeout,
    )
    completed = _now_ns()
    flow = {
        "mode": args.mode,
        "streaming": args.streaming,
        "scenario": args.scenario,
        "transport": "external" if args.ingest_only else "websocket",
        "entrypoint": args.entrypoint,
        "source": "live-runtime",
        "request_id": request_id,
        "session_id": session_id,
        "channel_id": channel_id,
        "started_unix_nano": invocation_started,
        "completed_unix_nano": completed,
        **outcome,
    }
    evidence["flows"] = _dedupe(
        [*evidence.get("flows", []), flow],
        ("mode", "streaming", "scenario", "entrypoint"),
    )
    evidence["spans"] = _dedupe(
        [*evidence.get("spans", []), *trace_spans],
        ("trace_id", "span_id"),
    )
    evidence["metrics"] = _dedupe(
        [*evidence.get("metrics", []), *metric_points],
        ("name", "time_unix_nano", "attributes"),
    )
    evidence["source"] = {
        "kind": "live-otel-collector",
        "trace_path": str(args.trace_json.resolve()),
        "metric_path": str(args.metric_json.resolve()),
    }
    evidence["generated_at_unix_nano"] = completed
    evidence["generated_at"] = datetime.now(timezone.utc).isoformat()
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return evidence_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode", required=True, choices=("code.normal", "agent.plan", "team")
    )
    stream = parser.add_mutually_exclusive_group(required=True)
    stream.add_argument("--stream", dest="streaming", action="store_true")
    stream.add_argument("--no-stream", dest="streaming", action="store_false")
    parser.add_argument(
        "--scenario",
        choices=("success", "cancel", "stuck", "tool_error"),
        default="success",
    )
    parser.add_argument(
        "--entrypoint",
        choices=("gateway", "team-runner"),
        default="gateway",
    )
    parser.add_argument(
        "--gateway-url",
        default=os.getenv("TELEMETRY_GATEWAY_WS_URL", "ws://127.0.0.1:19000/ws"),
    )
    parser.add_argument("--trace-json", type=Path, default=DEFAULT_TRACE_JSON)
    parser.add_argument("--metric-json", type=Path, default=DEFAULT_METRIC_JSON)
    parser.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--collector-timeout", type=float, default=30.0)
    parser.add_argument("--mock-llm-port", type=int, default=0)
    parser.add_argument("--new-run", action="store_true")
    parser.add_argument(
        "--ingest-only",
        action="store_true",
        help="ingest a request already driven through Team instead of opening WebSocket",
    )
    parser.add_argument("--request-id")
    parser.add_argument("--session-id")
    parser.add_argument("--channel-id")
    return parser


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    args = build_parser().parse_args()
    if args.ingest_only and (not args.request_id or not args.session_id):
        _LOGGER.error(
            "[telemetry-e2e] ERROR: --ingest-only requires --request-id and --session-id"
        )
        return 2
    if args.entrypoint != "gateway" and not args.ingest_only:
        _LOGGER.error(
            "[telemetry-e2e] ERROR: non-gateway entrypoints require "
            "--ingest-only with real request/session IDs"
        )
        return 2
    try:
        path = asyncio.run(run(args))
    except (
        EvidenceError,
        OSError,
        asyncio.TimeoutError,
        websockets.WebSocketException,
    ) as error:
        _LOGGER.error("[telemetry-e2e] ERROR: %s", error)
        return 1
    _LOGGER.info("[telemetry-e2e] fresh evidence: %s", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
