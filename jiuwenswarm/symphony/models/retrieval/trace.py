from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Dict, List


@dataclass(frozen=True)
class RetrieverTraceEvent:
    event_type: str
    node_id: str
    depth: int
    detail: Dict[str, object] = field(default_factory=dict)


@dataclass
class RetrieverTrace:
    events: List[RetrieverTraceEvent] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, event_type: str, *, node_id: str, depth: int, detail: Dict[str, object] | None = None) -> None:
        payload = dict(detail or {})
        with self._lock:
            self.events.append(RetrieverTraceEvent(event_type=event_type, node_id=node_id, depth=depth, detail=payload))

__all__ = ["RetrieverTrace", "RetrieverTraceEvent"]
