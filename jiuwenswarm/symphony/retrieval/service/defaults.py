from __future__ import annotations

from typing import Dict

from models.retrieval import RetrieverTraceEvent


def serialize_trace_event(event: RetrieverTraceEvent) -> Dict[str, object]:
    return {
        "event_type": str(event.event_type),
        "node_id": str(event.node_id),
        "depth": int(event.depth),
        "detail": dict(event.detail or {}),
    }


def serialize_hit_summary(choice_id: str, payload: str, rank: int, score: float) -> Dict[str, object]:
    return {
        "choice_id": str(choice_id),
        "payload": str(payload),
        "rank": int(rank),
        "score": float(score),
    }


__all__ = [
    "serialize_hit_summary",
    "serialize_trace_event",
]
