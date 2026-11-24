# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from jiuwenswarm.perf.accumulator import RequestMeta, RequestSummaryAccumulator
from jiuwenswarm.perf.config import get_perf_summary_config
from jiuwenswarm.perf.events import LlmPerfEvent, TaskPerfEvent, ToolPerfEvent
from jiuwenswarm.perf.writer import append_request_summary
from jiuwenswarm.perf.guard import run_perf_safe


def _normalize_tenant_scope_id(value: str | None, *, default: str = "default") -> str:
    if value is None:
        return default
    stripped = str(value).strip()
    return stripped or default


logger = logging.getLogger(__name__)


class PerfCollector:
    """In-memory request summary aggregator with process-wide routing."""

    _MAX_WRITTEN_IDS = 4096
    _MAX_FINALIZED_SUMMARIES = 4096
    _STALE_ORPHAN_AGE_S = 7200.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._accumulators: dict[str, RequestSummaryAccumulator] = {}
        self._active_request_ids: set[str] = set()
        self._written_request_ids: set[str] = set()
        self._inflight_finalize_ids: set[str] = set()
        # Finalized summaries retained for the pipeline's monitoring summary
        # log (#6745). finalize_request pops the accumulator, but the pipeline
        # finally-block runs AFTER the adapter's finalize — without this cache
        # the pipeline could no longer read the summary. Consumed via
        # pop_finalized_summary(); insert-order eviction bounds memory.
        self._finalized_summaries: dict[str, dict[str, Any]] = {}
        # session_key → (request_id, ask_wall_time); written at finalize of a
        # request that emitted an ask_user card, consumed by the following
        # resume request to report cross-request HITL wait.
        self._hitl_asks: dict[str, tuple[str, float]] = {}

    def begin_request(
        self,
        *,
        session_id: str,
        request_id: str,
        channel_id: str,
        mode: str,
        trace_id: str | None = None,
        started_at: float | None = None,
        service_id: str | None = None,
        agent_id: str | None = None,
    ) -> RequestSummaryAccumulator | None:
        cfg = get_perf_summary_config()
        if not cfg.enabled:
            return None
        rid = (request_id or "").strip()
        if not rid:
            return None

        from jiuwenswarm.perf.todo_tracker import clear_todo_tracker

        meta = RequestMeta(
            session_id=(session_id or "default").strip() or "default",
            request_id=rid,
            channel_id=(channel_id or "").strip(),
            mode=(mode or "agent.plan").strip() or "agent.plan",
            trace_id=trace_id,
            started_at=started_at if started_at is not None else time.time(),
            service_id=_normalize_tenant_scope_id(service_id),
            agent_id=_normalize_tenant_scope_id(agent_id),
        )
        acc = RequestSummaryAccumulator(
            meta=meta,
            _bottleneck_top_n=cfg.bottleneck_top_n,
            _include_errors=cfg.include_errors,
            _include_by_agent=cfg.include_by_agent,
        )
        with self._lock:
            self._prune_stale_accumulators_locked()
            self._active_request_ids.add(rid)
            existing = self._accumulators.get(rid)
            if existing is not None:
                return existing
            self._accumulators[rid] = acc
        # Fresh request must not inherit leftover todo tracking for a reused id.
        clear_todo_tracker(rid)
        return acc

    def get_accumulator(self, request_id: str) -> RequestSummaryAccumulator | None:
        rid = (request_id or "").strip()
        if not rid:
            return None
        with self._lock:
            return self._accumulators.get(rid)

    def record_llm(self, request_id: str, event: LlmPerfEvent) -> None:
        acc = self.get_accumulator(request_id)
        if acc is None:
            return
        acc.record_llm(event)

    def record_external_token_usage(
        self,
        request_id: str,
        *,
        source: str,
        usage_id: str,
        input_tokens: int,
        output_tokens: int,
        total_tokens: int,
    ) -> bool:
        acc = self.get_accumulator(request_id)
        if acc is None:
            return False
        return acc.record_external_token_usage(
            source=source,
            usage_id=usage_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        )

    def record_tool(self, request_id: str, event: ToolPerfEvent) -> None:
        acc = self.get_accumulator(request_id)
        if acc is None:
            return
        acc.record_tool(event)

    def record_task(self, request_id: str, event: TaskPerfEvent) -> None:
        cfg = get_perf_summary_config()
        if not cfg.include_tasks:
            return
        acc = self.get_accumulator(request_id)
        if acc is None:
            return
        acc.record_task(event)

    def mark_first_byte_latency(self, request_id: str, latency_ms: float) -> None:
        acc = self.get_accumulator(request_id)
        if acc is None:
            return
        acc.set_first_byte_latency_ms(latency_ms)

    def mark_first_answer_latency(self, request_id: str, latency_ms: float) -> None:
        acc = self.get_accumulator(request_id)
        if acc is None:
            return
        acc.set_first_answer_latency_ms(latency_ms)

    def mark_stage(self, request_id: str, stage: str) -> None:
        """First-mark-wins stage timestamp (monotonic) for the summary log."""
        acc = self.get_accumulator(request_id)
        if acc is None:
            return
        acc.mark_stage(stage, time.monotonic())

    def pop_finalized_summary(self, request_id: str) -> dict[str, Any] | None:
        """Consume the retained finalized summary for this request (once)."""
        rid = (request_id or "").strip()
        if not rid:
            return None
        with self._lock:
            return self._finalized_summaries.pop(rid, None)

    def pop_hitl_ask(
        self,
        session_id: str,
        *,
        exclude_request_id: str | None = None,
    ) -> tuple[str, float] | None:
        """Consume the pending ask_user registration for this session.

        ``exclude_request_id`` keeps the entry in place (returns None) when the
        pending ask was registered by the request currently finalizing — the
        ask line itself must not consume its own registration.
        """
        key = (session_id or "").strip()
        if not key:
            return None
        exclude = (exclude_request_id or "").strip()
        with self._lock:
            entry = self._hitl_asks.get(key)
            if entry is None or (exclude and entry[0] == exclude):
                return None
            return self._hitl_asks.pop(key)

    def record_llm_stream_timing(
        self,
        request_id: str,
        *,
        ttft_ms: float | None,
        total_ms: float,
        chunk_count: int,
        status: str,
    ) -> None:
        acc = self.get_accumulator(request_id)
        if acc is None:
            return
        acc.record_llm_stream_timing(
            ttft_ms=ttft_ms,
            total_ms=total_ms,
            chunk_count=chunk_count,
            status=status,
        )

    def record_skill_usage(self, request_id: str, skill_name: str) -> None:
        acc = self.get_accumulator(request_id)
        if acc is None:
            return
        acc.record_skill_usage(skill_name)

    def record_context_usage(
        self,
        request_id: str,
        *,
        limit_tokens: Any,
        occupancy_rate: Any,
    ) -> None:
        acc = self.get_accumulator(request_id)
        if acc is None:
            return
        acc.record_context_usage(limit_tokens=limit_tokens, occupancy_rate=occupancy_rate)

    def record_chat_error(self, request_id: str) -> None:
        acc = self.get_accumulator(request_id)
        if acc is None:
            return
        acc.record_chat_error()

    def record_chunk_sent(self, request_id: str) -> None:
        acc = self.get_accumulator(request_id)
        if acc is None:
            return
        acc.record_chunk_sent()

    def mark_llm_in_flight(self, request_id: str, active: bool) -> None:
        acc = self.get_accumulator(request_id)
        if acc is None:
            return
        acc.set_llm_in_flight(active)

    def mark_tool_in_flight(self, request_id: str, active: bool) -> None:
        acc = self.get_accumulator(request_id)
        if acc is None:
            return
        acc.set_tool_in_flight(active)

    def mark_ask_emitted(self, request_id: str) -> None:
        acc = self.get_accumulator(request_id)
        if acc is None:
            return
        acc.set_ask_emitted()

    def record_thinking_segment(self, request_id: str, duration_ms: float) -> None:
        acc = self.get_accumulator(request_id)
        if acc is None:
            return
        acc.record_thinking_segment(duration_ms)

    def record_message_gen(self, request_id: str, duration_ms: float) -> None:
        acc = self.get_accumulator(request_id)
        if acc is None:
            return
        acc.record_message_gen(duration_ms)

    def record_stream_gap(self, request_id: str, duration_ms: float) -> None:
        acc = self.get_accumulator(request_id)
        if acc is None:
            return
        acc.record_stream_gap(duration_ms)

    def _register_hitl_ask_locked(self, rid: str, session_id: str, ask_wall: float) -> None:
        if not session_id:
            return
        self._hitl_asks[session_id] = (rid, ask_wall)
        overflow = len(self._hitl_asks) - self._MAX_FINALIZED_SUMMARIES
        while overflow > 0 and self._hitl_asks:
            self._hitl_asks.pop(next(iter(self._hitl_asks)))
            overflow -= 1

    def _retain_finalized_summary_locked(self, rid: str, summary: dict[str, Any]) -> None:
        self._finalized_summaries[rid] = summary
        overflow = len(self._finalized_summaries) - self._MAX_FINALIZED_SUMMARIES
        while overflow > 0 and self._finalized_summaries:
            self._finalized_summaries.pop(next(iter(self._finalized_summaries)))
            overflow -= 1

    def finalize_request(
        self,
        request_id: str,
        *,
        status: str = "ok",
        ended_at: float | None = None,
    ) -> None:
        cfg = get_perf_summary_config()
        if not cfg.enabled:
            return

        rid = (request_id or "").strip()
        if not rid:
            return

        from jiuwenswarm.perf.todo_tracker import clear_todo_tracker

        try:
            with self._lock:
                # Skip duplicate / concurrent finalize for the same request_id.
                if rid in self._written_request_ids or rid in self._inflight_finalize_ids:
                    self._active_request_ids.discard(rid)
                    return
                acc = self._accumulators.pop(rid, None)
                self._active_request_ids.discard(rid)
                if acc is not None and not acc.flushed:
                    self._inflight_finalize_ids.add(rid)

            if acc is None or acc.flushed:
                if acc is None:
                    logger.warning(
                        "finalize_request: accumulator missing for request_id=%s",
                        rid,
                    )
                return

            try:
                from jiuwenswarm.perf.context import get_request_wall_start

                wall_start = get_request_wall_start()
                if acc.first_byte_latency_ms is None and wall_start is not None:
                    acc.set_first_byte_latency_ms(
                        max(0.0, (time.time() - wall_start) * 1000)
                    )

                summary = acc.finalize(status=status, ended_at=ended_at)

                # Retain for the pipeline's monitoring summary log (#6745),
                # regardless of whether the jsonl write below succeeds.
                with self._lock:
                    self._retain_finalized_summary_locked(rid, summary)
                    if acc.ask_emitted:
                        self._register_hitl_ask_locked(
                            rid,
                            acc.meta.session_id,
                            acc.ended_at if acc.ended_at is not None else time.time(),
                        )

                def _write_summary() -> None:
                    append_request_summary(
                        acc.meta.session_id,
                        summary,
                        service_id=acc.meta.service_id,
                        agent_id=acc.meta.agent_id,
                    )
                    acc.flushed = True
                    with self._lock:
                        self._written_request_ids.add(rid)
                        self._trim_written_request_ids_locked()

                run_perf_safe("PerfCollector", "request summary write", _write_summary)
                if not acc.flushed:
                    with self._lock:
                        if rid not in self._written_request_ids:
                            self._accumulators.setdefault(rid, acc)
            finally:
                with self._lock:
                    self._inflight_finalize_ids.discard(rid)
        finally:
            # Always drop todo tracking for this request (success, miss, or retry).
            clear_todo_tracker(rid)

    def _prune_stale_accumulators_locked(self) -> None:
        now = time.time()
        stale: list[str] = []
        for rid, acc in self._accumulators.items():
            if rid in self._active_request_ids:
                continue
            if not acc.meta.started_at:
                continue
            if now - acc.meta.started_at <= self._STALE_ORPHAN_AGE_S:
                continue
            stale.append(rid)
        for rid in stale:
            self._accumulators.pop(rid, None)
            logger.warning(
                "pruned orphan accumulator request_id=%s",
                rid,
            )

    def _trim_written_request_ids_locked(self) -> None:
        overflow = len(self._written_request_ids) - self._MAX_WRITTEN_IDS
        while overflow > 0 and self._written_request_ids:
            self._written_request_ids.pop()
            overflow -= 1


_COLLECTOR = PerfCollector()


def get_perf_collector() -> PerfCollector:
    return _COLLECTOR
