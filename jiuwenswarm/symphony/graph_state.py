"""Persistent state for Symphony graph builds."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from openjiuwen.symphony import CapabilityDescriptor, CapabilityFingerprint

from jiuwenswarm.symphony.graph_storage import resolve_graph_artifact_dir

GRAPH_STATE_FILENAME = "graph_state.json"


@dataclass(frozen=True)
class GraphStateEntry:
    """State tracked for one Skill folder in a graph build."""

    skill_id: str
    relative_path: str
    content_hash: str
    fingerprint_hash: str
    status: str = "active"
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "relative_path": self.relative_path,
            "content_hash": self.content_hash,
            "fingerprint_hash": self.fingerprint_hash,
            "status": self.status,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "GraphStateEntry":
        return cls(
            skill_id=str(payload.get("skill_id") or ""),
            relative_path=str(payload.get("relative_path") or ""),
            content_hash=str(
                payload.get("content_hash") or payload.get("skill_md_sha256") or ""
            ),
            fingerprint_hash=str(payload.get("fingerprint_hash") or ""),
            status=str(payload.get("status") or "active"),
            updated_at=str(payload.get("updated_at") or ""),
        )


@dataclass(frozen=True)
class GraphState:
    """Serializable incremental graph state."""

    schema_version: str = "Symphony-graph-state-v2"
    skills: dict[str, GraphStateEntry] = field(default_factory=dict)

    def active_entries(self) -> dict[str, GraphStateEntry]:
        return {
            path: entry
            for path, entry in self.skills.items()
            if entry.status == "active"
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "skills": {
                path: entry.to_dict() for path, entry in sorted(self.skills.items())
            },
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "GraphState":
        raw_skills = payload.get("skills") or {}
        skills: dict[str, GraphStateEntry] = {}
        if isinstance(raw_skills, dict):
            for key, value in raw_skills.items():
                if not isinstance(value, dict):
                    continue
                entry = GraphStateEntry.from_dict(value)
                relative_path = entry.relative_path or str(key)
                skills[relative_path] = GraphStateEntry(
                    skill_id=entry.skill_id,
                    relative_path=relative_path,
                    content_hash=entry.content_hash,
                    fingerprint_hash=entry.fingerprint_hash,
                    status=entry.status,
                    updated_at=entry.updated_at,
                )
        return cls(
            schema_version=str(
                payload.get("schema_version") or "Symphony-graph-state-v1"
            ),
            skills=skills,
        )


class GraphStateBuilder:
    """Map a core capability inventory to JiuwenSwarm graph state."""

    def capability_hashes(
        self,
        capabilities: Iterable[CapabilityDescriptor],
    ) -> dict[str, str]:
        return {
            self.relative_path(capability): capability.content_hash
            for capability in capabilities
        }

    def next_state(
        self,
        *,
        capabilities: Iterable[CapabilityDescriptor],
        current_hashes: dict[str, str],
        fingerprints_by_id: Mapping[str, CapabilityFingerprint],
        old_state: GraphState,
        removed_paths: set[str],
    ) -> GraphState:
        now = datetime.now(timezone.utc).isoformat()
        entries: dict[str, GraphStateEntry] = {}
        for capability in capabilities:
            relative_path = self.relative_path(capability)
            fingerprint = fingerprints_by_id[capability.capability_id]
            entries[relative_path] = GraphStateEntry(
                skill_id=fingerprint.capability_id,
                relative_path=relative_path,
                content_hash=current_hashes[relative_path],
                fingerprint_hash=self.fingerprint_hash(fingerprint),
                status="active",
                updated_at=now,
            )

        for relative_path in sorted(removed_paths):
            old_entry = old_state.skills.get(relative_path)
            if old_entry is None:
                continue
            entries[relative_path] = GraphStateEntry(
                skill_id=old_entry.skill_id,
                relative_path=relative_path,
                content_hash=old_entry.content_hash,
                fingerprint_hash=old_entry.fingerprint_hash,
                status="removed",
                updated_at=now,
            )
        return GraphState(skills=entries)

    @staticmethod
    def fingerprint_hash(fingerprint: CapabilityFingerprint) -> str:
        payload = json.dumps(
            fingerprint.graph_identity_dict(),
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def relative_path(capability: CapabilityDescriptor) -> str:
        entrypoint = str(capability.metadata.get("entrypoint") or "").strip()
        if not entrypoint:
            raise ValueError(
                f"Capability {capability.capability_id!r} has no entrypoint metadata."
            )
        return Path(entrypoint).parent.as_posix()


def load_graph_state(graph_dir: str | Path) -> GraphState:
    """Load graph_state.json, returning an empty state when it is missing."""

    path = resolve_graph_artifact_dir(graph_dir) / GRAPH_STATE_FILENAME
    if not path.exists():
        return GraphState()
    return GraphState.from_dict(json.loads(path.read_text(encoding="utf-8")))


def write_graph_state(state: GraphState, graph_dir: str | Path) -> None:
    """Write graph_state.json with stable formatting."""

    output_path = Path(graph_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    (output_path / GRAPH_STATE_FILENAME).write_text(
        json.dumps(state.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
