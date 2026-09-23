# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from jiuwenswarm.perf.events import LlmPerfEvent, TaskPerfEvent, ToolPerfEvent
from jiuwenswarm.perf.stats import (
    LlmStatsAccumulator,
    ToolStatsAccumulator,
    maintain_top_n,
    ms_to_s,
)


@dataclass
class RequestMeta:
    session_id: str
    request_id: str
    channel_id: str
    mode: str
    trace_id: str | None
    started_at: float
    service_id: str = "default"
    agent_id: str = "default"

    def with_trace_id(self, trace_id: str | None) -> RequestMeta:
        if trace_id:
            return RequestMeta(
                session_id=self.session_id,
                request_id=self.request_id,
                channel_id=self.channel_id,
                mode=self.mode,
                trace_id=trace_id,
                started_at=self.started_at,
                service_id=self.service_id,
                agent_id=self.agent_id,
            )
        return self


@dataclass
class RequestSummaryAccumulator:
    meta: RequestMeta
    status: str = "ok"
    first_byte_latency_ms: int | None = None
    first_answer_latency_ms: int | None = None
    ended_at: float | None = None
    llm_stats: LlmStatsAccumulator = field(default_factory=LlmStatsAccumulator)
    tool_stats: ToolStatsAccumulator = field(default_factory=ToolStatsAccumulator)
    task_count: int = 0
    task_total_ms: float = 0.0
    task_fail_count: int = 0
    unattributed_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_read_tokens: int = 0
    reasoning_tokens: int = 0
    tasks: list[dict[str, Any]] = field(default_factory=list)
    bottleneck_llm: list[dict[str, Any]] = field(default_factory=list)
    bottleneck_tool: list[dict[str, Any]] = field(default_factory=list)
    bottleneck_task: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    flushed: bool = False
    _bottleneck_top_n: int = 3
    _include_errors: bool = False
    _include_by_agent: bool = False
    _recorded_task_ids: set[str] = field(default_factory=set)
    _task_stats: dict[str, tuple[LlmStatsAccumulator, ToolStatsAccumulator]] = field(
        default_factory=dict
    )
    _agent_llm: dict[str, LlmStatsAccumulator] = field(default_factory=dict)
    _agent_tool: dict[str, ToolStatsAccumulator] = field(default_factory=dict)
    _agent_tokens: dict[str, dict[str, int]] = field(default_factory=dict)
    _recorded_external_usage_ids: set[tuple[str, str]] = field(default_factory=set)

    def _task_stat_pair(self, task_id: str) -> tuple[LlmStatsAccumulator, ToolStatsAccumulator]:
        pair = self._task_stats.get(task_id)
        if pair is None:
            pair = (LlmStatsAccumulator(), ToolStatsAccumulator())
            self._task_stats[task_id] = pair
        return pair

    @staticmethod
    def _agent_key(agent_id: str | None) -> str:
        return (agent_id or "").strip() or "unknown"

    def _agent_token_bucket(self, agent_id: str | None) -> dict[str, int]:
        key = self._agent_key(agent_id)
        bucket = self._agent_tokens.get(key)
        if bucket is None:
            bucket = {"input": 0, "output": 0, "reasoning": 0}
            self._agent_tokens[key] = bucket
        return bucket

    def _append_error(self, entry: dict[str, Any]) -> None:
        if not self._include_errors:
            return
        self.errors.append(entry)

    def set_first_byte_latency_ms(self, latency_ms: float) -> None:
        """First history-visible assistant event (tool_call / reasoning / answer)."""
        if self.first_byte_latency_ms is None and latency_ms >= 0:
            self.first_byte_latency_ms = int(round(latency_ms))

    def set_first_answer_latency_ms(self, latency_ms: float) -> None:
        """First answer token (chat.delta / chat.final), excluding tool/reasoning."""
        if self.first_answer_latency_ms is None and latency_ms >= 0:
            self.first_answer_latency_ms = int(round(latency_ms))

    def record_llm(self, event: LlmPerfEvent) -> None:
        self.llm_stats.record(
            duration_ms=event.duration_ms,
            status=event.status,
        )
        self.input_tokens += max(0, event.input_tokens)
        self.output_tokens += max(0, event.output_tokens)
        self.total_tokens += max(0, event.input_tokens) + max(0, event.output_tokens)
        self.reasoning_tokens += max(0, event.reasoning_tokens)
        if self._include_by_agent:
            agent_key = self._agent_key(event.agent_id)
            self._agent_llm.setdefault(agent_key, LlmStatsAccumulator()).record(
                duration_ms=event.duration_ms,
                status=event.status,
            )
            token_bucket = self._agent_token_bucket(agent_key)
            token_bucket["input"] += max(0, event.input_tokens)
            token_bucket["output"] += max(0, event.output_tokens)
            token_bucket["reasoning"] += max(0, event.reasoning_tokens)
        if event.task_id:
            llm_acc, _ = self._task_stat_pair(event.task_id)
            llm_acc.record(
                duration_ms=event.duration_ms,
                status=event.status,
            )
        else:
            self.unattributed_ms += event.duration_ms

        entry: dict[str, Any] = {
            "duration_s": ms_to_s(event.duration_ms),
            "agent_id": event.agent_id,
            "task_id": event.task_id,
            "model": event.model,
            "iteration": event.iteration,
            "input_tokens": event.input_tokens,
            "output_tokens": event.output_tokens,
            "reasoning_tokens": event.reasoning_tokens,
        }
        if event.stream_source_id:
            entry["stream_source_id"] = event.stream_source_id
        if event.status != "ok":
            error_entry: dict[str, Any] = {
                "kind": "llm",
                "status": "error",
                "name": event.model or "unknown",
                "error": event.error_message or "unknown error",
                "task_id": event.task_id,
                "agent_id": event.agent_id,
                "iteration": event.iteration,
                "duration_s": ms_to_s(event.duration_ms),
            }
            self._append_error(error_entry)
        self.bottleneck_llm = maintain_top_n(
            self.bottleneck_llm,
            entry,
            top_n=self._bottleneck_top_n,
        )

    def record_external_token_usage(
        self,
        *,
        source: str,
        usage_id: str,
        input_tokens: int,
        output_tokens: int,
        total_tokens: int,
    ) -> bool:
        """Add one external aggregate without fabricating an LLM timing event."""
        key = (source.strip(), usage_id.strip())
        if not all(key) or key in self._recorded_external_usage_ids:
            return False
        self._recorded_external_usage_ids.add(key)
        self.input_tokens += max(0, input_tokens)
        self.output_tokens += max(0, output_tokens)
        self.total_tokens += max(0, total_tokens)
        return True

    def record_tool(self, event: ToolPerfEvent) -> None:
        self.tool_stats.record(
            duration_ms=event.duration_ms,
            status=event.status,
            name=event.name,
            iteration=event.iteration,
        )
        if self._include_by_agent:
            agent_key = self._agent_key(event.agent_id)
            self._agent_tool.setdefault(agent_key, ToolStatsAccumulator()).record(
                duration_ms=event.duration_ms,
                status=event.status,
                name=event.name,
                iteration=event.iteration,
            )
        if event.task_id:
            _, tool_acc = self._task_stat_pair(event.task_id)
            tool_acc.record(
                duration_ms=event.duration_ms,
                status=event.status,
                name=event.name,
                iteration=event.iteration,
            )
        else:
            self.unattributed_ms += event.duration_ms
        if event.status != "ok":
            error_entry: dict[str, Any] = {
                "kind": "tool",
                "status": "error",
                "name": event.name or "unknown",
                "call_id": event.tool_call_id,
                "error": event.error_message or "unknown error",
                "task_id": event.task_id,
                "agent_id": event.agent_id,
                "iteration": event.iteration,
                "duration_s": ms_to_s(event.duration_ms),
            }
            self._append_error(error_entry)
        entry = {
            "duration_s": ms_to_s(event.duration_ms),
            "name": event.name,
            "tool_call_id": event.tool_call_id,
            "agent_id": event.agent_id,
            "task_id": event.task_id,
            "iteration": event.iteration,
        }
        self.bottleneck_tool = maintain_top_n(
            self.bottleneck_tool,
            entry,
            top_n=self._bottleneck_top_n,
        )

    def record_task(self, event: TaskPerfEvent) -> None:
        # Dedupe by task_id within one request (safe if callers retry).
        if event.task_id in self._recorded_task_ids:
            return
        self._recorded_task_ids.add(event.task_id)

        self.task_count += 1
        self.task_total_ms += event.duration_ms
        if event.status not in ("completed", "succeeded", "ok"):
            self.task_fail_count += 1

        task_stats = self._task_stats.pop(event.task_id, None)
        if task_stats is None:
            llm_stats = LlmStatsAccumulator()
            tool_stats = ToolStatsAccumulator()
        else:
            llm_stats, tool_stats = task_stats

        task_entry = {
            "order": len(self.tasks) + 1,
            "task_id": event.task_id,
            "task_content": event.task_content,
            "source": event.source,
            "started_at": event.started_at,
            "ended_at": event.ended_at,
            "duration_s": ms_to_s(event.duration_ms),
            "status": event.status,
            "stats": {
                "llm": llm_stats.to_dict(),
                "tool": tool_stats.to_dict(),
            },
        }
        self.tasks.append(task_entry)
        task_rank_entry = {
            "duration_s": ms_to_s(event.duration_ms),
            "task_id": event.task_id,
            "task_content": event.task_content,
        }
        self.bottleneck_task = maintain_top_n(
            self.bottleneck_task,
            task_rank_entry,
            top_n=self._bottleneck_top_n,
        )

    def _by_agent_dict(self) -> dict[str, Any]:
        agent_ids = sorted(
            set(self._agent_llm) | set(self._agent_tool) | set(self._agent_tokens)
        )
        out: dict[str, Any] = {}
        for agent_id in agent_ids:
            llm = self._agent_llm.get(agent_id) or LlmStatsAccumulator()
            tool = self._agent_tool.get(agent_id) or ToolStatsAccumulator()
            tokens = self._agent_tokens.get(agent_id) or {
                "input": 0,
                "output": 0,
                "reasoning": 0,
            }
            out[agent_id] = {
                "llm": llm.to_dict(),
                "tool": tool.to_dict(),
                "tokens": {
                    "input": int(tokens.get("input") or 0),
                    "output": int(tokens.get("output") or 0),
                    "reasoning": int(tokens.get("reasoning") or 0),
                },
            }
        return out

    def finalize(self, *, status: str | None = None, ended_at: float | None = None) -> dict[str, Any]:
        if status is not None:
            self.status = status
        self.ended_at = ended_at if ended_at is not None else time.time()
        total_s = ms_to_s(max(0.0, (self.ended_at - self.meta.started_at) * 1000))
        # Nested parent/subagent durations can sum above wall clock; clamp.
        unattributed_s = min(ms_to_s(self.unattributed_ms), total_s)
        if self.first_byte_latency_ms is None:
            first_byte_latency_s = 0.0
        else:
            first_byte_latency_s = ms_to_s(float(self.first_byte_latency_ms))
        if self.first_answer_latency_ms is None:
            first_answer_latency_s = 0.0
        else:
            first_answer_latency_s = ms_to_s(float(self.first_answer_latency_ms))

        summary: dict[str, Any] = {
            "schema_version": 1,
            "meta": {
                "session_id": self.meta.session_id,
                "request_id": self.meta.request_id,
                "channel_id": self.meta.channel_id,
                "mode": self.meta.mode,
                "trace_id": self.meta.trace_id,
                "started_at": self.meta.started_at,
                "ended_at": self.ended_at,
            },
            "summary": {
                "total_s": total_s,
                "first_byte_latency_s": first_byte_latency_s,
                "first_answer_latency_s": first_answer_latency_s,
                "status": self.status,
                "stats": {
                    "llm": self.llm_stats.to_dict(),
                    "tool": self.tool_stats.to_dict(),
                    "task": {
                        "count": self.task_count,
                        "total_s": ms_to_s(self.task_total_ms),
                        "fail_count": self.task_fail_count,
                    },
                    "unattributed_s": unattributed_s,
                },
                "tokens": {
                    "input": self.input_tokens,
                    "output": self.output_tokens,
                    "total": self.total_tokens,
                    "cache_read": self.cache_read_tokens,
                    "reasoning": self.reasoning_tokens,
                },
            },
            "tasks": self.tasks,
            "bottleneck": {
                "task": self.bottleneck_task,
                "llm": self.bottleneck_llm,
                "tool": self.bottleneck_tool,
            },
        }
        if self._include_by_agent:
            summary["summary"]["stats"]["by_agent"] = self._by_agent_dict()
        if self._include_errors:
            summary["errors"] = self.errors
        return summary
