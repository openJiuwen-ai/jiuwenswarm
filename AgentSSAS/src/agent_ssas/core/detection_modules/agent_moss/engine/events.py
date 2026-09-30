# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import time
import uuid
from typing import Any


DecisionName = str


@dataclass
class PolicyDecision:
    decision: DecisionName = "allow"
    risk_score: int = 0
    findings: list[str] = field(default_factory=list)
    reason: str = ""

    @property
    def should_block(self) -> bool:
        return self.decision == "block"


@dataclass
class EventRecord:
    event_type: str
    subject: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    session_id: str = ""
    request_id: str = ""
    agent_name: str = ""
    source: str = "agentmoss"
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: float = field(default_factory=time.time)
    risk_score: int = 0
    decision: DecisionName = "allow"
    findings: list[str] = field(default_factory=list)
    prev_event_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)


def bounded_jsonable(value: Any, max_chars: int = 12000) -> Any:
    """Return a JSON-safe, bounded representation for evidence storage."""
    try:
        json.dumps(value, ensure_ascii=False)
        safe = value
    except TypeError:
        safe = repr(value)

    text = json.dumps(safe, ensure_ascii=False, default=repr)
    if len(text) <= max_chars:
        return safe
    return {
        "_truncated": True,
        "preview": text[:max_chars],
        "original_chars": len(text),
    }
