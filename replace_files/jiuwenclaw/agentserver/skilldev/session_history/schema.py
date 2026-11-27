from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class SkillDevSessionEventRecord:
    seq: int
    timestamp: str
    source: str
    event_type: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "timestamp": self.timestamp,
            "source": self.source,
            "event_type": self.event_type,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SkillDevSessionEventRecord":
        return cls(
            seq=int(data.get("seq", 0)),
            timestamp=str(data.get("timestamp", "")),
            source=str(data.get("source", "system")),
            event_type=str(data.get("event_type", "")),
            payload=dict(data.get("payload") or {}),
        )


@dataclass
class SkillDevSessionSummary:
    task_id: str
    stage: str
    updated_at: str
    created_at: str
    is_suspended: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "stage": self.stage,
            "updated_at": self.updated_at,
            "created_at": self.created_at,
            "is_suspended": self.is_suspended,
        }
