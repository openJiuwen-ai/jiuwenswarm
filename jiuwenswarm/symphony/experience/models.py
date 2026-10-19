from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ExperienceItem:
    """One entry in the experience base.

    Each item represents a class of queries that map to one or more skills.
    """

    id: str
    query_pattern: str = ""
    query_examples: list[str] = field(default_factory=list)
    skill_ids: list[str] = field(default_factory=list)
    success_count: int = 1
    embedding: list[float] = field(default_factory=list)
    created_at: float = 0.0
    last_hit_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "query_pattern": self.query_pattern,
            "query_examples": self.query_examples,
            "skill_ids": self.skill_ids,
            "success_count": self.success_count,
            "created_at": self.created_at,
            "last_hit_at": self.last_hit_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ExperienceItem:
        return cls(
            id=data["id"],
            query_pattern=data.get("query_pattern", ""),
            query_examples=list(data.get("query_examples", [])),
            skill_ids=list(data.get("skill_ids", [])),
            success_count=int(data.get("success_count", 1)),
            embedding=list(data.get("embedding", [])),
            created_at=float(data.get("created_at", 0.0)),
            last_hit_at=float(data.get("last_hit_at", 0.0)),
        )


@dataclass
class TraceRecord:
    trace_id: str
    query: str = ""
    skills: list[str] = field(default_factory=list)
    messages: list[dict] = field(default_factory=list)
    result: str = ""
    error_type: str | None = None
    error_detail: str | None = None
    success: bool = False

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "query": self.query,
            "skills": self.skills,
            "messages": self.messages,
            "result": self.result,
            "error_type": self.error_type,
            "error_detail": self.error_detail,
            "success": self.success,
        }

    @classmethod
    def from_dict(cls, data: dict) -> TraceRecord:
        return cls(
            trace_id=data["trace_id"],
            query=data.get("query", ""),
            skills=list(data.get("skills", [])),
            messages=list(data.get("messages", [])),
            result=data.get("result", ""),
            error_type=data.get("error_type"),
            error_detail=data.get("error_detail"),
            success=bool(data.get("success", False)),
        )


@dataclass
class ExperienceBankBuildConfig:
    """Configuration for ExperienceBaseBuilder pipeline."""

    min_cluster_size: int = 1
    max_workers: int = 8
    max_success_examples: int = 20
    pending_flush_threshold: int = 20
    min_hits_for_pattern: int = 1
    skill_cluster_num: int | None = None
    pattern_merge_threshold: float = 0.9
    query_examples_count: int = 5


@dataclass
class DistilledPattern:
    """Distilled knowledge pattern from one cluster."""

    cluster_id: int
    effective_skills: list[str] = field(default_factory=list)
    pattern_description: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-serializable dict."""
        return {
            "cluster_id": self.cluster_id,
            "effective_skills": self.effective_skills,
            "pattern_description": self.pattern_description,
        }

    @classmethod
    def from_dict(cls, d: dict) -> DistilledPattern:
        """Deserialize from a dict."""
        return cls(
            cluster_id=d["cluster_id"],
            effective_skills=list(d.get("effective_skills", [])),
            pattern_description=str(d.get("pattern_description", "")),
        )