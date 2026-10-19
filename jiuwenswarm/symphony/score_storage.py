"""Versioned storage helpers for Symphony Score artifacts."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CURRENT_POINTER_FILENAME = "current.json"
VERSIONS_DIRNAME = "versions"
BUILD_RUNS_DIRNAME = ".build_runs"


@dataclass(frozen=True)
class ScorePointer:
    """Pointer to the currently published Score version."""

    version: str
    path: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "Symphony-score-pointer-v1",
            "version": self.version,
            "path": self.path,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ScorePointer":
        version = str(payload.get("version") or "").strip()
        path = str(payload.get("path") or "").strip()
        if not version or not path:
            raise ValueError("Invalid Symphony score pointer.")
        return cls(version=version, path=path)


def resolve_score_artifact_dir(score_dir: str | Path) -> Path:
    """Return the directory containing the currently readable artifacts.

    New builds publish versioned artifacts behind ``current.json``. Older
    layouts stored artifacts directly in ``score_dir``; this helper keeps that
    legacy layout readable.
    """

    root = Path(score_dir).resolve()
    pointer_path = root / CURRENT_POINTER_FILENAME
    if not pointer_path.is_file():
        return root

    pointer = ScorePointer.from_dict(
        json.loads(pointer_path.read_text(encoding="utf-8"))
    )
    candidate = (root / pointer.path).resolve()
    if not _is_relative_to(candidate, root):
        raise ValueError(f"Symphony score pointer escapes score_dir: {candidate}")
    return candidate


def score_manifest_path(score_dir: str | Path) -> Path:
    return resolve_score_artifact_dir(score_dir) / "score_manifest.json"


def score_exists(score_dir: str | Path) -> bool:
    return score_manifest_path(score_dir).is_file()


def build_runs_dir(score_dir: str | Path) -> Path:
    return Path(score_dir).resolve() / BUILD_RUNS_DIRNAME


def build_run_dir(score_dir: str | Path, run_id: str) -> Path:
    return build_runs_dir(score_dir) / run_id


def build_artifact_dir(score_dir: str | Path, run_id: str) -> Path:
    return build_run_dir(score_dir, run_id) / "artifacts"


def publish_artifact_dir(
    score_dir: str | Path,
    artifact_dir: str | Path,
    *,
    version: str,
) -> Path:
    """Publish a fully built artifact directory as the current Score version."""

    root = Path(score_dir).resolve()
    source = Path(artifact_dir).resolve()
    versions_dir = root / VERSIONS_DIRNAME
    versions_dir.mkdir(parents=True, exist_ok=True)
    target = versions_dir / version
    if target.exists():
        raise FileExistsError(f"Symphony score version already exists: {target}")
    os.replace(source, target)

    pointer = ScorePointer(
        version=version,
        path=f"{VERSIONS_DIRNAME}/{version}",
    )
    tmp_pointer = root / f".{CURRENT_POINTER_FILENAME}.{version}.tmp"
    tmp_pointer.write_text(
        json.dumps(pointer.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp_pointer, root / CURRENT_POINTER_FILENAME)
    return target


def latest_incomplete_build(score_dir: str | Path) -> Path | None:
    runs_root = build_runs_dir(score_dir)
    if not runs_root.is_dir():
        return None
    candidates = []
    for child in runs_root.iterdir():
        checkpoint = child / "checkpoint.json"
        if not checkpoint.is_file():
            continue
        try:
            payload = json.loads(checkpoint.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        candidates.append(
            (
                str(payload.get("updated_at") or ""),
                str(payload.get("status") or ""),
                child,
            )
        )
    if not candidates:
        return None
    _updated_at, status, path = sorted(candidates, key=lambda item: item[0])[-1]
    if status in {"running", "failed"}:
        return path
    return None


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
