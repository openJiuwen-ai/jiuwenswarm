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
    _STALE_ORPHAN_AGE_S = 7200.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._accumulators: dict[str, RequestSummaryAccumulator] = {}
        self._active_request_ids: set[str] = set()
        self._written_request_ids: set[str] = set()
        self._inflight_finalize_ids: set[str] = set()

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
