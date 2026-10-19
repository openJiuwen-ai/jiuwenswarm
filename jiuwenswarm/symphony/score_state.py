"""Persistent state for Symphony Score builds."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from jiuwenswarm.symphony.fingerprint.models import Fingerprint
from jiuwenswarm.symphony.score_storage import resolve_score_artifact_dir

SCORE_STATE_FILENAME = "score_state.json"


@dataclass(frozen=True)
class ScoreStateEntry:
    """State tracked for one Skill folder in a Score build."""

    skill_id: str
    relative_path: str
    skill_md_sha256: str
    fingerprint_hash: str
    status: str = "active"
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "relative_path": self.relative_path,
            "skill_md_sha256": self.skill_md_sha256,
            "fingerprint_hash": self.fingerprint_hash,
            "status": self.status,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ScoreStateEntry":
        return cls(
            skill_id=str(payload.get("skill_id") or ""),
            relative_path=str(payload.get("relative_path") or ""),
            skill_md_sha256=str(payload.get("skill_md_sha256") or ""),
            fingerprint_hash=str(payload.get("fingerprint_hash") or ""),
            status=str(payload.get("status") or "active"),
            updated_at=str(payload.get("updated_at") or ""),
        )


@dataclass(frozen=True)
class ScoreState:
    """Serializable incremental Score state."""

    schema_version: str = "Symphony-score-state-v1"
    skills: dict[str, ScoreStateEntry] = field(default_factory=dict)

    def active_entries(self) -> dict[str, ScoreStateEntry]:
        return {
            path: entry
            for path, entry in self.skills.items()
            if entry.status == "active"
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "skills": {
                path: entry.to_dict()
                for path, entry in sorted(self.skills.items())
            },
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ScoreState":
        raw_skills = payload.get("skills") or {}
        skills: dict[str, ScoreStateEntry] = {}
        if isinstance(raw_skills, dict):
            for key, value in raw_skills.items():
                if not isinstance(value, dict):
                    continue
                entry = ScoreStateEntry.from_dict(value)
                relative_path = entry.relative_path or str(key)
                skills[relative_path] = ScoreStateEntry(
                    skill_id=entry.skill_id,
                    relative_path=relative_path,
                    skill_md_sha256=entry.skill_md_sha256,
                    fingerprint_hash=entry.fingerprint_hash,
                    status=entry.status,
                    updated_at=entry.updated_at,
                )
        return cls(
            schema_version=str(payload.get("schema_version") or "Symphony-score-state-v1"),
            skills=skills,
        )


class ScoreStateBuilder:
    """Compute folder hashes and the next persistent Score state."""

    def folder_hashes(self, folders: Iterable[Any]) -> dict[str, str]:
        return {
            folder.relative_path: self.file_sha256(folder.entry)
            for folder in folders
        }

    def next_state(
        self,
        *,
        folders: list[Any],
        current_hashes: dict[str, str],
        fingerprints_by_path: dict[str, Fingerprint],
        old_state: ScoreState,
        removed_paths: set[str],
    ) -> ScoreState:
        now = datetime.now(timezone.utc).isoformat()
        entries: dict[str, ScoreStateEntry] = {}
        for folder in folders:
            fingerprint = fingerprints_by_path[folder.relative_path]
            entries[folder.relative_path] = ScoreStateEntry(
                skill_id=fingerprint.id,
                relative_path=folder.relative_path,
                skill_md_sha256=current_hashes[folder.relative_path],
                fingerprint_hash=self.fingerprint_hash(fingerprint),
                status="active",
                updated_at=now,
            )

        for relative_path in sorted(removed_paths):
            old_entry = old_state.skills.get(relative_path)
            if old_entry is None:
                continue
            entries[relative_path] = ScoreStateEntry(
                skill_id=old_entry.skill_id,
                relative_path=relative_path,
                skill_md_sha256=old_entry.skill_md_sha256,
                fingerprint_hash=old_entry.fingerprint_hash,
                status="removed",
                updated_at=now,
            )
        return ScoreState(skills=entries)

    @staticmethod
    def fingerprint_hash(fingerprint: Fingerprint) -> str:
        payload = json.dumps(
            fingerprint.graph_identity_dict(),
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def file_sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()


def load_score_state(score_dir: str | Path) -> ScoreState:
    """Load score_state.json, returning an empty state when it is missing."""

    path = resolve_score_artifact_dir(score_dir) / SCORE_STATE_FILENAME
    if not path.exists():
        return ScoreState()
    return ScoreState.from_dict(json.loads(path.read_text(encoding="utf-8")))


def write_score_state(state: ScoreState, score_dir: str | Path) -> None:
    """Write score_state.json with stable formatting."""

    output_path = Path(score_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    (output_path / SCORE_STATE_FILENAME).write_text(
        json.dumps(state.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
