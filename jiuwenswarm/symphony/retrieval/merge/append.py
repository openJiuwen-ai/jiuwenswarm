from __future__ import annotations

from typing import Dict, List, Sequence

from models.retrieval import RetrieverCandidate, RetrieverTrace
from ..tree.types import ProgressiveRetrieverResult

from ..service.models import SearchResult


def hits_to_search_result(
    *,
    method: str,
    source: str,
    elapsed_ms: float,
    trace_events: List[Dict[str, object]],
    candidate_records: List[Dict[str, object]],
) -> SearchResult:
    payloads = [str(record.get("resolved_payload") or "") for record in candidate_records]
    return SearchResult(
        method=method,
        payloads=payloads,
        candidate_records=candidate_records,
        summary_lines=[
            _format_append_summary_line(index, record, source)
            for index, record in enumerate(candidate_records, start=1)
        ],
        selected_payload=payloads[0] if payloads else None,
        selected_rank=1 if payloads else -1,
        elapsed_ms=float(elapsed_ms),
        trace_events=trace_events,
    )


def _format_append_summary_line(index: int, record: dict[str, object], source: str) -> str:
    choice_id = record.get("choice_id") or record.get("raw_output") or ""
    resolved_payload = record.get("resolved_payload") or ""
    return f"{index}. {choice_id} -> {resolved_payload} (source={source})"


__all__ = [
    "hits_to_search_result",
]
