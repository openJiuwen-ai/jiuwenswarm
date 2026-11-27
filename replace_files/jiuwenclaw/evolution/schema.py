# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Data model definitions for skill evolution system."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional

VALID_SECTIONS = {"Instructions", "Examples", "Troubleshooting"}


class EvolutionType(str, Enum):
    """Evolution type that determines which handler processes the signal."""

    SKILL_EXPERIENCE = "skill_experience"
    NEW_SKILL = "new_skill"


class ExperienceTarget(str, Enum):
    """Which layer of the skill the experience targets."""

    DESCRIPTION = "description"
    BODY = "body"


@dataclass
class EvolutionChange:
    section: str    # "Instructions" | "Examples" | "Troubleshooting"
    action: str     # "append" | "skip"
    content: str    # Markdown content to append
    target: ExperienceTarget = ExperienceTarget.BODY
    skip_reason: Optional[str] = None  # "irrelevant" | "duplicate" (only when action=="skip")
    merge_target: Optional[str] = None  # existing entry id to replace (dedup merge)

    def to_dict(self) -> dict:
        d = {
            "section": self.section,
            "action": self.action,
            "content": self.content,
            "target": self.target.value,
        }
        if self.skip_reason:
            d["skip_reason"] = self.skip_reason
        if self.merge_target:
            d["merge_target"] = self.merge_target
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "EvolutionChange":
        raw_target = d.get("target", "body")
        try:
            target = ExperienceTarget(raw_target)
        except ValueError:
            target = ExperienceTarget.BODY
        return cls(
            section=d.get("section", "Troubleshooting"),
            action=d.get("action", "append"),
            content=d.get("content", ""),
            target=target,
            skip_reason=d.get("skip_reason"),
            merge_target=d.get("merge_target"),
        )


@dataclass
class EvolutionEntry:
    id: str                    # "ev_xxxxxxxx"
    source: str                # "execution_failure" | "user_correction" | "repeated_failure"
    timestamp: str             # ISO 8601
    context: str               # Signal summary
    change: EvolutionChange
    applied: bool = False      # False = pending, True = solidified

    @classmethod
    def make(
        cls,
        source: str,
        context: str,
        change: EvolutionChange,
    ) -> "EvolutionEntry":
        return cls(
            id=f"ev_{uuid.uuid4().hex[:8]}",
            source=source,
            timestamp=datetime.now(tz=timezone.utc).isoformat(),
            context=context,
            change=change,
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "source": self.source,
            "timestamp": self.timestamp,
            "context": self.context,
            "change": self.change.to_dict(),
            "applied": self.applied,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "EvolutionEntry":
        return cls(
            id=d.get("id", f"ev_{uuid.uuid4().hex[:8]}"),
            source=d.get("source", "unknown"),
            timestamp=d.get("timestamp", ""),
            context=d.get("context", ""),
            change=EvolutionChange.from_dict(d.get("change", {})),
            applied=d.get("applied", False),
        )

    @property
    def is_pending(self) -> bool:
        return not self.applied


@dataclass
class EvolutionFile:
    skill_id: str
    version: str = "1.0.0"
    updated_at: str = field(
        default_factory=lambda: datetime.now(tz=timezone.utc).isoformat()
    )
    entries: List[EvolutionEntry] = field(default_factory=list)

    @property
    def pending_entries(self) -> List[EvolutionEntry]:
        return [e for e in self.entries if e.is_pending]

    def to_dict(self) -> dict:
        return {
            "skill_id": self.skill_id,
            "version": self.version,
            "updated_at": self.updated_at,
            "entries": [e.to_dict() for e in self.entries],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "EvolutionFile":
        entries = [EvolutionEntry.from_dict(e) for e in d.get("entries", [])]
        return cls(
            skill_id=d.get("skill_id", ""),
            version=d.get("version", "1.0.0"),
            updated_at=d.get("updated_at", ""),
            entries=entries,
        )

    @classmethod
    def empty(cls, skill_id: str) -> "EvolutionFile":
        return cls(skill_id=skill_id)


@dataclass
class ExperienceContext:
    """All inputs needed for LLM-based experience generation."""

    skill_name: str
    signals: List["EvolutionSignal"]
    skill_content: str
    messages: List[dict]
    existing_desc_entries: List[EvolutionEntry]
    existing_body_entries: List[EvolutionEntry]


@dataclass
class EvolutionSignal:
    type: str                      # "execution_failure" | "user_correction" | "repeated_failure"
    evolution_type: EvolutionType  # determines which handler processes this signal
    section: str                   # Recommended SKILL.md section
    excerpt: str                   # Original content summary
    tool_name: Optional[str] = None
    skill_name: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "evolution_type": self.evolution_type.value,
            "section": self.section,
            "excerpt": self.excerpt,
            "tool_name": self.tool_name,
            "skill_name": self.skill_name,
        }
