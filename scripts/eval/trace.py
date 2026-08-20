# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Per-instance eval trace: every tool/LLM call plus Code Graph engine metrics."""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openjiuwen.harness.rails.base import DeepAgentRail

from trajectory import TrajectoryRecorder

# Every Code Graph tool the graph profile can expose. Counted so
# ``graph_tool_calls`` stays comparable across runs.
_GRAPH_TOOLS = frozenset(
    {
        "resolve_symbol",
        "find_code_symbols",
        "search_source_text",
        "inspect_code_structure",
        "read_symbol",
        "read_code",
        "find_callers",
        "find_callees",
        "find_importers",
        "find_base_classes",
        "find_subclasses",
        "trace_call_paths",
        "select_code_context",
        "submit_code_context",
    }
)

# Per-tool counters in the trace summary.
_COUNTED_TOOLS = (
    "grep",
    "read_file",
    "task_tool",
    "bash",
    "resolve_symbol",
    "find_code_symbols",
    "search_source_text",
    "inspect_code_structure",
    "read_symbol",
    "find_callers",
    "find_callees",
    "find_importers",
    "find_base_classes",
    "find_subclasses",
    "trace_call_paths",
    "select_code_context",
    "submit_code_context",
)

# List-valued keys worth counting in a tool summary, across every graph tool.
_LIST_PAYLOAD_KEYS = (
    "matches",
    "symbols",
    "related",
    "chunks",
    "locations",
    "paths",
    "direct_callers",
    "transitive_callers",
    "subclasses",
    "implementations",
    "imports",
    "tests",
    "unresolved",
    "candidates",
    # analyze_patch_impact: the graph-level review of an edit.
    "changed_symbols",
    "added_symbols",
    "removed_symbols",
    "test_candidates",
    "unwired_symbols",
    "dangling_references",
)

_SUMMED_TOTALS = (
    "find_code_symbols_calls",
    "resolve_symbol_calls",
    "submit_code_context_calls",
    "graph_tool_calls",
    "grep_calls",
    "llm_calls",
    "prompt_tokens",
    "completion_tokens",
    "duplicate_search_calls",
    "truncated_results",
    "next_actions_offered",
    "next_actions_adopted",
    "edit_calls",
    "bash_calls",
)

# Tools that turn a candidate into evidence. Anything else after a search is
# still searching, which is the pattern Run A has to show going down.
_EVIDENCE_TOOLS = frozenset(
    {
        "read_file",
        "read_code",
        "read_symbol",
        "inspect_code_structure",
        "find_callers",
        "find_callees",
        "find_importers",
        "find_base_classes",
        "find_subclasses",
        "trace_call_paths",
        "select_code_context",
        "submit_code_context",
        "edit_file",
        "write_file",
    }
)
_SEARCH_TOOLS = frozenset(
    {
        "grep",
        "find_code_symbols",
        "search_source_text",
        "resolve_symbol",
    }
)
_EDIT_TOOLS = frozenset({"edit_file", "write_file"})


def aggregate_traces(output_dir: Path) -> Path:
    """Write traces.jsonl + trace_summary.json from per-instance *.trace.json."""
    traces_path = output_dir / "traces.jsonl"
    summary_path = output_dir / "trace_summary.json"
    records: list[dict[str, Any]] = []
    kept_paths: list[Path] = []
    for path in sorted(output_dir.glob("*.trace.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"warning: skip {path}: {exc}", file=sys.stderr)
            continue
        records.append(payload)
        kept_paths.append(path)
    with traces_path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    totals = {
        "instances": len(records),
        "wall_ms": round(sum(float(item.get("wall_ms") or 0) for item in records), 3),
        "llm_ms": round(
            sum(
                float((item.get("totals") or {}).get("llm_ms") or 0) for item in records
            ),
            3,
        ),
        "tool_ms": round(
            sum(
                float((item.get("totals") or {}).get("tool_ms") or 0)
                for item in records
            ),
            3,
        ),
        "index_build_ms": round(
            sum(
                float(
                    ((item.get("code_graph") or {}).get("totals") or {}).get(
                        "index_build_ms"
                    )
                    or 0
                )
                for item in records
            ),
            3,
        ),
        "query_ms": round(
            sum(
                float(
                    ((item.get("code_graph") or {}).get("totals") or {}).get("query_ms")
                    or 0
                )
                for item in records
            ),
            3,
        ),
    }
    for key in _SUMMED_TOTALS:
        totals[key] = sum(
            int((item.get("totals") or {}).get(key) or 0) for item in records
        )
    summary_path.write_text(
        json.dumps(
            {
                "arm_dir": str(output_dir),
                "totals": totals,
                "instances": [
                    {
                        "file": path.name,
                        "instance_id": path.name.replace(".trace.json", ""),
                        "wall_ms": item.get("wall_ms"),
                        "totals": item.get("totals"),
                        "code_graph_totals": (item.get("code_graph") or {}).get(
                            "totals"
                        ),
                    }
                    for path, item in zip(kept_paths, records)
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"aggregated {len(records)} traces -> {traces_path}", flush=True)
    return traces_path


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    data = getattr(value, "data", None)
    if isinstance(data, dict):
        return data
    return {}


def _usage_from_response(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage", None) or getattr(
        response, "usage_metadata", None
    )
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    if usage is None:
        return {}
    if isinstance(usage, dict):
        prompt = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
        completion = usage.get("completion_tokens") or usage.get("output_tokens") or 0
    else:
        prompt = (
            getattr(usage, "prompt_tokens", None)
            or getattr(usage, "input_tokens", 0)
            or 0
        )
        completion = (
            getattr(usage, "completion_tokens", None)
            or getattr(usage, "output_tokens", 0)
            or 0
        )
    return {"prompt_tokens": int(prompt), "completion_tokens": int(completion)}


def _response_text(response: Any) -> str:
    if response is None:
        return ""
    content = getattr(response, "content", None)
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(response, dict):
        raw = response.get("content") or response.get("text") or ""
        return raw if isinstance(raw, str) else ""
    return str(getattr(response, "text", "") or "")


def _as_args(value: Any) -> dict[str, Any]:
    """Tool arguments as a dict.

    The engine hands rails ``ToolCall.arguments``, which is the raw JSON string
    from the model. Reading it as a dict silently dropped every argument, so a
    trajectory showed that `bash` ran but not what it ran.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def summarize_tool_payload(name: str, args: Any, result: Any) -> dict[str, Any]:
    payload = _as_dict(result)
    summary: dict[str, Any] = {"tool": name, "status": payload.get("status")}
    parsed_args = _as_args(args)
    if parsed_args:
        for key in (
            "query",
            "name",
            "file",
            "file_path",
            "symbol_id",
            "subagent_type",
            "pattern",
            "path_hint",
        ):
            if parsed_args.get(key) not in (None, ""):
                summary[key] = parsed_args[key]
        if parsed_args.get("command"):
            summary["command"] = str(parsed_args["command"])[:400]
    if payload.get("file") and "file" not in summary:
        summary["file"] = payload.get("file")
    if payload.get("start_line") is not None:
        summary["start_line"] = payload.get("start_line")
        summary["end_line"] = payload.get("end_line")
    for key in _LIST_PAYLOAD_KEYS:
        items = payload.get(key)
        if isinstance(items, list):
            summary[f"{key}_count"] = len(items)
            summary[key] = items[:20]
    # Risk level on a tool payload, if present.
    risk = payload.get("risk")
    if isinstance(risk, dict):
        summary["risk_level"] = risk.get("level")
        summary["risk_reasons"] = risk.get("reasons")
    # submit_code_context: keep the shape of the handoff, not its whole body.
    packet = payload.get("context_packet")
    if isinstance(packet, dict):
        summary["context_packet"] = {
            "artifact_id": packet.get("artifact_id"),
            "file_count": packet.get("file_count"),
            "span_count": packet.get("span_count"),
        }
    # next_actions is the routing signal: what the tool proposed, so the next
    # event can show whether the model took it.
    actions = payload.get("next_actions")
    if isinstance(actions, list):
        summary["next_actions"] = [
            {
                "tool": str(item.get("tool") or ""),
                "symbol_id": item.get("symbol_id"),
                "file": item.get("file"),
                "must_before": item.get("must_before"),
            }
            for item in actions
            if isinstance(item, dict) and item.get("tool")
        ]
    if payload.get("duplicate_query"):
        summary["duplicate_query"] = True
    if payload.get("phase"):
        summary["phase"] = payload.get("phase")
    if payload.get("truncated"):
        summary["truncated"] = True
    if payload.get("message"):
        summary["message"] = payload.get("message")
    # Shell output tail: the only place a test verdict can be read from.
    content = payload.get("content")
    if isinstance(content, str) and content:
        summary["output_tail"] = content[-2000:]
    summary["succeeded"] = bool(getattr(result, "success", True))
    if payload.get("index_snapshot"):
        summary["index_snapshot"] = payload.get("index_snapshot")
    if payload.get("index_revision") is not None:
        summary["index_revision"] = payload.get("index_revision")
    # Warnings carry the stale-graph signal, which decides whether a graph answer
    # can be trusted after an edit.
    warnings = payload.get("warnings")
    if isinstance(warnings, list) and warnings:
        summary["warnings"] = [str(item) for item in warnings[:10]]
    return summary


def _offer_from_trace(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return item
    return {"tool": str(item)}


def _event_matches_offer(offer: dict[str, Any], event: dict[str, Any]) -> bool:
    """Same tool and, when the offer named one, the same file or symbol."""
    if str(offer.get("tool") or "") != str(event.get("tool") or ""):
        return False
    for key in ("symbol_id", "file"):
        wanted = str(offer.get(key) or "").strip()
        if not wanted:
            continue
        given = " ".join(
            str(event.get(name) or "")
            for name in ("symbol_id", "file", "file_path", "path", "absolute_path")
        )
        if wanted not in given.replace("\\", "/"):
            return False
    return True


def process_metrics(tool_events: list[dict[str, Any]]) -> dict[str, Any]:
    """Derive locate-exam process metrics from the recorded tool events."""
    metrics = {
        "duplicate_search_calls": 0,
        "truncated_results": 0,
        "next_actions_offered": 0,
        "next_actions_adopted": 0,
        "edit_calls": 0,
        "max_search_streak": 0,
        "first_hit_to_first_evidence": None,
        "first_hit_to_first_edit": None,
    }
    streak = 0
    first_hit: int | None = None
    pending_actions: list[dict[str, Any]] = []
    for index, event in enumerate(tool_events):
        name = str(event.get("tool") or "")
        if event.get("duplicate_query"):
            metrics["duplicate_search_calls"] += 1
        if event.get("truncated"):
            metrics["truncated_results"] += 1
        actions = event.get("next_actions")
        if isinstance(actions, list) and actions:
            metrics["next_actions_offered"] += 1
            pending_actions = [_offer_from_trace(item) for item in actions]
        elif pending_actions:
            remaining: list[dict[str, Any]] = []
            adopted = False
            for offer in pending_actions:
                if not adopted and _event_matches_offer(offer, event):
                    metrics["next_actions_adopted"] += 1
                    adopted = True
                    continue
                remaining.append(offer)
            pending_actions = remaining
        if name in _SEARCH_TOOLS:
            streak += 1
            metrics["max_search_streak"] = max(metrics["max_search_streak"], streak)
            if first_hit is None and int(event.get("matches_count") or 0) > 0:
                first_hit = index
        elif name in _EVIDENCE_TOOLS:
            streak = 0
            if first_hit is not None and metrics["first_hit_to_first_evidence"] is None:
                metrics["first_hit_to_first_evidence"] = index - first_hit
        if name in _EDIT_TOOLS:
            metrics["edit_calls"] += 1
            if first_hit is not None and metrics["first_hit_to_first_edit"] is None:
                metrics["first_hit_to_first_edit"] = index - first_hit
    return metrics


@dataclass
class EvalTrace:
    repo_root: str = ""
    flags: dict[str, Any] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    wall_started: float = field(default_factory=time.perf_counter)
    tool_events: list[dict[str, Any]] = field(default_factory=list)
    llm_events: list[dict[str, Any]] = field(default_factory=list)
    recorder: TrajectoryRecorder = field(default_factory=TrajectoryRecorder)
    _tool_started: dict[int, float] = field(default_factory=dict)
    _llm_started: dict[int, float] = field(default_factory=dict)
    agents: list[Any] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        if not self.recorder.repo_root:
            self.recorder.repo_root = self.repo_root
        try:
            from openjiuwen.core.retrieval.code_graph.metrics import (
                reset_code_graph_metrics,
            )

            reset_code_graph_metrics()
        except ImportError:
            pass

    def make_rail(self) -> "EvalTraceRail":
        return EvalTraceRail(self)

    def finish(self, *, output: Any = None) -> dict[str, Any]:
        wall_ms = (time.perf_counter() - self.wall_started) * 1000
        graph = {}
        try:
            from openjiuwen.core.retrieval.code_graph.metrics import (
                snapshot_code_graph_metrics,
            )

            graph = snapshot_code_graph_metrics()
        except ImportError:
            graph = {"events": [], "totals": {}}
        tool_ms = sum(float(item.get("duration_ms") or 0) for item in self.tool_events)
        llm_ms = sum(float(item.get("duration_ms") or 0) for item in self.llm_events)
        prompt_tokens = sum(
            int(item.get("prompt_tokens") or 0) for item in self.llm_events
        )
        completion_tokens = sum(
            int(item.get("completion_tokens") or 0) for item in self.llm_events
        )
        names = [str(item.get("tool") or "") for item in self.tool_events]
        totals: dict[str, Any] = {
            "llm_calls": len(self.llm_events),
            "llm_ms": round(llm_ms, 3),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "tool_calls": len(self.tool_events),
            "tool_ms": round(tool_ms, 3),
        }
        for name in _COUNTED_TOOLS:
            totals[f"{name}_calls"] = names.count(name)
        totals["graph_tool_calls"] = sum(1 for name in names if name in _GRAPH_TOOLS)
        totals.update(process_metrics(self.tool_events))
        return {
            "flags": self.flags,
            "wall_ms": round(wall_ms, 3),
            "started_at": self.started_at,
            "totals": totals,
            "llm_events": self.llm_events,
            "tool_events": self.tool_events,
            "code_graph": graph,
            "output_preview": str(output or "")[:4000],
        }


class EvalTraceRail(DeepAgentRail):
    """Record LLM and tool timings for one agent (root or subagent)."""

    priority = 9

    def __init__(self, trace: EvalTrace) -> None:
        super().__init__()
        self.trace = trace

    def init(self, agent: Any) -> None:
        super().init(agent)
        if agent is not None and all(existing is not agent for existing in self.trace.agents):
            self.trace.agents.append(agent)

    async def before_model_call(self, ctx: Any) -> None:
        self.trace._llm_started[id(ctx)] = time.perf_counter()

    async def after_model_call(self, ctx: Any) -> None:
        started = self.trace._llm_started.pop(id(ctx), None)
        duration_ms = (time.perf_counter() - started) * 1000 if started else 0.0
        inputs = getattr(ctx, "inputs", None)
        agent = getattr(ctx, "agent", None)
        card = getattr(agent, "card", None)
        usage = _usage_from_response(getattr(inputs, "response", None))
        event: dict[str, Any] = {
            "duration_ms": round(duration_ms, 3),
            "agent": getattr(card, "name", None),
            "tool_count": len(getattr(inputs, "tools", None) or []),
        }
        event.update(usage)
        text = _response_text(getattr(inputs, "response", None))
        if text:
            event["content"] = text[:8000]
            if self.trace.recorder.mode == "contextbench":
                self.trace.recorder.apply_texts([text])
        self.trace.llm_events.append(event)

    async def before_tool_call(self, ctx: Any) -> None:
        self.trace._tool_started[id(ctx)] = time.perf_counter()

    async def after_tool_call(self, ctx: Any) -> None:
        started = self.trace._tool_started.pop(id(ctx), None)
        duration_ms = (time.perf_counter() - started) * 1000 if started else 0.0
        inputs = getattr(ctx, "inputs", None)
        name = getattr(inputs, "tool_name", "") or ""
        args = getattr(inputs, "tool_args", None)
        result = getattr(inputs, "tool_result", None)
        event = summarize_tool_payload(name, args, result)
        event["duration_ms"] = round(duration_ms, 3)
        agent = getattr(ctx, "agent", None)
        card = getattr(agent, "card", None)
        event["agent"] = getattr(card, "name", None)
        self.trace.tool_events.append(event)
        self.trace.recorder.record(name, args, result)
