"""Incremental fingerprint reuse support."""

from __future__ import annotations

import json
import hashlib
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from jiuwenswarm.symphony.fingerprint.models import (
    ExtractionDiagnostic,
    Fingerprint,
    NormalizationDecision,
    NormalizationResult,
    SkillFolder,
)
from jiuwenswarm.symphony.fingerprint.normalize import (
    IONameVocabulary,
    SkillFingerprintNormalizer,
)
from jiuwenswarm.symphony.score_state import load_score_state
from jiuwenswarm.symphony.score_storage import resolve_score_artifact_dir

IndexedSkillFolder = tuple[int, SkillFolder]


@dataclass(frozen=True)
class FingerprintReusePlan:
    changed_folders: list[IndexedSkillFolder]
    fingerprints: list[Fingerprint]
    fingerprints_by_path: dict[str, Fingerprint]
    removed_paths: set[str]
    reused_count: int


class IncrementalFingerprintStore:
    """Load prior Score artifacts and decide what must be re-extracted."""

    def __init__(self, output_dir: Path, *, signature: str = "") -> None:
        self.output_dir = output_dir
        self.signature = signature
        self.cache = FingerprintResultCache(output_dir, signature=signature)
        self.old_state = load_score_state(output_dir)
        self.old_fingerprints = load_fingerprints(output_dir)
        self.old_fingerprints_by_id = {item.id: item for item in self.old_fingerprints}

    def plan(
        self,
        folders: list[SkillFolder],
        current_hashes: dict[str, str],
        *,
        force: bool,
        on_reuse: Callable[[int, SkillFolder, Fingerprint], None] | None = None,
    ) -> FingerprintReusePlan:
        old_active_entries = self.old_state.active_entries()
        removed_paths = {
            relative_path
            for relative_path in old_active_entries
            if relative_path not in current_hashes
        }
        changed_folders: list[IndexedSkillFolder] = []
        fingerprints: list[Fingerprint] = []
        fingerprints_by_path: dict[str, Fingerprint] = {}
        reused_count = 0

        for folder_index, folder in enumerate(folders):
            old_entry = old_active_entries.get(folder.relative_path)
            if (
                not force
                and old_entry is not None
                and old_entry.skill_md_sha256 == current_hashes[folder.relative_path]
            ):
                old_fingerprint = self.old_fingerprints_by_id.get(old_entry.skill_id)
                if old_fingerprint is not None:
                    fingerprints.append(old_fingerprint)
                    fingerprints_by_path[folder.relative_path] = old_fingerprint
                    reused_count += 1
                    if on_reuse is not None:
                        on_reuse(folder_index, folder, old_fingerprint)
                    continue
            cached = None if force else self.cache.load(
                folder.relative_path,
                current_hashes[folder.relative_path],
            )
            if cached is not None:
                fingerprints.append(cached.fingerprint)
                fingerprints_by_path[folder.relative_path] = cached.fingerprint
                reused_count += 1
                if on_reuse is not None:
                    on_reuse(folder_index, folder, cached.fingerprint)
                continue
            changed_folders.append((folder_index, folder))

        return FingerprintReusePlan(
            changed_folders=changed_folders,
            fingerprints=fingerprints,
            fingerprints_by_path=fingerprints_by_path,
            removed_paths=removed_paths,
            reused_count=reused_count,
        )

    def save_result(
        self,
        folder: SkillFolder,
        skill_md_sha256: str,
        result: NormalizationResult,
    ) -> None:
        self.cache.save(folder.relative_path, skill_md_sha256, result)


class FingerprintResultCache:
    """Per-skill normalized fingerprint cache.

    The cache is intentionally outside published Score artifacts. A failed build
    may still leave valid per-skill extraction results that can be reused by the
    next build.
    """

    def __init__(self, score_dir: Path, *, signature: str = "") -> None:
        self.score_dir = Path(score_dir).resolve()
        self.signature = signature
        self.root = self.score_dir / "cache" / "fingerprints"

    def load(
        self,
        relative_path: str,
        skill_md_sha256: str,
    ) -> NormalizationResult | None:
        path = self._path(relative_path)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if str(payload.get("skill_md_sha256") or "") != skill_md_sha256:
            return None
        if str(payload.get("signature") or "") != self.signature:
            return None
        fingerprint_payload = payload.get("fingerprint")
        if not isinstance(fingerprint_payload, dict):
            return None
        return NormalizationResult(
            fingerprint=Fingerprint.from_dict(fingerprint_payload),
            diagnostics=[
                _diagnostic_from_dict(item)
                for item in payload.get("diagnostics", [])
                if isinstance(item, dict)
            ],
            decisions=[
                _decision_from_dict(item)
                for item in payload.get("decisions", [])
                if isinstance(item, dict)
            ],
        )

    def save(
        self,
        relative_path: str,
        skill_md_sha256: str,
        result: NormalizationResult,
    ) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": "Symphony-fingerprint-cache-v1",
            "relative_path": relative_path,
            "skill_md_sha256": skill_md_sha256,
            "signature": self.signature,
            "fingerprint": result.fingerprint.to_dict(),
            "diagnostics": [item.to_dict() for item in result.diagnostics],
            "decisions": [item.to_dict() for item in result.decisions],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        target = self._path(relative_path)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, target)

    def _path(self, relative_path: str) -> Path:
        digest = hashlib.sha256(relative_path.encode("utf-8")).hexdigest()
        return self.root / f"{digest}.json"


def load_fingerprints(output_dir: Path) -> list[Fingerprint]:
    path = resolve_score_artifact_dir(output_dir) / "fingerprints.json"
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [
        Fingerprint.from_dict(item)
        for item in payload.get("fingerprints", [])
        if isinstance(item, dict)
    ]


def load_io_name_vocabulary(
    output_dir: Path,
    normalizer: SkillFingerprintNormalizer,
) -> IONameVocabulary:
    path = resolve_score_artifact_dir(output_dir) / "io_name_vocab.json"
    if not path.exists():
        return IONameVocabulary.from_config(normalizer.config)
    return IONameVocabulary.load(path, normalizer.config)


def _diagnostic_from_dict(payload: dict[str, Any]) -> ExtractionDiagnostic:
    return ExtractionDiagnostic(
        stage=str(payload.get("stage") or ""),
        severity=str(payload.get("severity") or ""),
        code=str(payload.get("code") or ""),
        message=str(payload.get("message") or ""),
        skill_id=payload.get("skill_id"),
        path=payload.get("path"),
        details=payload.get("details") if isinstance(payload.get("details"), dict) else {},
    )


def _decision_from_dict(payload: dict[str, Any]) -> NormalizationDecision:
    return NormalizationDecision(
        skill_id=str(payload.get("skill_id") or ""),
        path=payload.get("path"),
        direction=str(payload.get("direction") or ""),
        field=str(payload.get("field") or ""),
        raw_value=str(payload.get("raw_value") or ""),
        token=str(payload.get("token") or ""),
        normalized_value=str(payload.get("normalized_value") or ""),
        method=str(payload.get("method") or ""),
        vocab=str(payload.get("vocab") or ""),
        vocab_version=str(payload.get("vocab_version") or ""),
        confidence=float(payload.get("confidence") or 0.0),
        details=payload.get("details") if isinstance(payload.get("details"), dict) else {},
    )
