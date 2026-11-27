from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, Sequence

from .models import RetrievalMethod, RetrieverConfig, RetrieverSearchResult


@dataclass(frozen=True)
class RetrievalRequest:
    query: str | Sequence[Dict[str, str]]
    top_k: int
    runtime_config: RetrieverConfig
    llm_top_k: int | None = None


@dataclass(frozen=True)
class RetrievalMethodContext:
    search_progressive: Any
    progressive_unavailable_reason: Any
    emit_fallback_event: Any


class BaseRetrievalMethod(ABC):
    method_name: str = ""

    def __init__(self, *, context: RetrievalMethodContext) -> None:
        self._context = context

    @abstractmethod
    def search(self, request: RetrievalRequest) -> RetrieverSearchResult:
        raise NotImplementedError

    def _search_progressive(self, request: RetrievalRequest) -> RetrieverSearchResult:
        return self._context.search_progressive(
            query=request.query,
            top_k=request.top_k,
            runtime_config=request.runtime_config,
        )

    def _progressive_unavailable_reason(self, runtime_config: RetrieverConfig) -> str | None:
        return self._context.progressive_unavailable_reason(runtime_config)

    def _emit_fallback_event(self, *, requested_method: str, fallback_method: str, reason: str) -> None:
        self._context.emit_fallback_event(
            requested_method=requested_method,
            fallback_method=fallback_method,
            reason=reason,
        )


class ProgressiveRetrievalMethod(BaseRetrievalMethod):
    method_name = RetrievalMethod.PROGRESSIVE.value

    def search(self, request: RetrievalRequest) -> RetrieverSearchResult:
        progressive_reason = self._progressive_unavailable_reason(request.runtime_config)
        if progressive_reason is not None:
            raise RuntimeError(f"progressive retrieval is unavailable: {progressive_reason}")
        return self._search_progressive(request)


class AutoRetrievalMethod(ProgressiveRetrievalMethod):
    method_name = RetrievalMethod.AUTO.value


def create_retrieval_method(method: str, *, context: RetrievalMethodContext) -> BaseRetrievalMethod:
    return ProgressiveRetrievalMethod(context=context)


def truncate_primary_result(
    result: RetrieverSearchResult, *, top_k: int, llm_top_k: int | None
) -> RetrieverSearchResult:
    if llm_top_k is None:
        limit = top_k
    else:
        limit = max(0, min(int(llm_top_k), int(top_k)))
    if limit >= len(result.candidate_records):
        return result
    candidate_records = [dict(record) for record in result.candidate_records[:limit]]
    for index, record in enumerate(candidate_records, start=1):
        record["rank"] = index
        record["selected"] = index == 1
    payloads = [str(record.get("resolved_payload") or "") for record in candidate_records]
    return RetrieverSearchResult(
        method=result.method,
        payloads=payloads,
        candidate_records=candidate_records,
        summary_lines=list(result.summary_lines[:limit]),
        selected_payload=payloads[0] if payloads else None,
        selected_rank=1 if payloads else -1,
        elapsed_ms=result.elapsed_ms,
        trace_events=list(result.trace_events),
    )


__all__ = [
    "AutoRetrievalMethod",
    "BaseRetrievalMethod",
    "ProgressiveRetrievalMethod",
    "RetrievalMethodContext",
    "RetrievalRequest",
    "create_retrieval_method",
    "truncate_primary_result",
]
