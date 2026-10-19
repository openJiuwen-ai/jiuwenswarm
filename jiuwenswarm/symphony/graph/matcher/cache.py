"""Persistent cache for ontology relation matching."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from jiuwenswarm.symphony.fingerprint.models import Fingerprint
from jiuwenswarm.symphony.graph.models import LLMMatch, RelationCandidate, SkillRegistry


@dataclass(frozen=True)
class RelationCacheStats:
    reused_count: int = 0
    resolved_count: int = 0
    stored_count: int = 0


class CachedOntologyMatcher:
    """Wrap an ontology matcher with a per-candidate persistent cache."""

    def __init__(
        self,
        matcher: Any,
        cache_path: str | Path,
        *,
        fingerprints: Iterable[Fingerprint],
    ) -> None:
        self.matcher = matcher
        self.cache = RelationMatchCache(
            cache_path,
            matcher_signature=_matcher_signature(matcher),
            fingerprints=fingerprints,
        )
        self.diagnostics = []
        self.stats = RelationCacheStats()

    async def match(
        self,
        registry: SkillRegistry,
        candidates: Iterable[RelationCandidate],
    ) -> list[LLMMatch]:
        candidate_list = list(candidates)
        cached_matches: list[tuple[int, list[LLMMatch]]] = []
        misses: list[tuple[int, RelationCandidate]] = []
        reused_count = 0
        for index, candidate in enumerate(candidate_list):
            matches = self.cache.load(candidate)
            if matches is None:
                misses.append((index, candidate))
                continue
            cached_matches.append((index, matches))
            reused_count += 1

        resolved_by_index: dict[int, list[LLMMatch]] = {}
        diagnostics: list[Any] = []
        if misses:
            # Preserve the wrapped matcher's worker-level concurrency while
            # retaining cache flush boundaries for resumable graph builds.
            for window in _chunked(misses, _matcher_window_size(self.matcher)):
                miss_candidates = [candidate for _, candidate in window]
                resolved_matches = list(
                    await self.matcher.match(registry, miss_candidates)
                )
                resolved_by_candidate = _matches_by_candidate(
                    miss_candidates,
                    resolved_matches,
                )
                for index, candidate in window:
                    matches = resolved_by_candidate.get(candidate.key, [])
                    resolved_by_index[index] = matches
                    self.cache.store(candidate, matches)
                self.cache.flush()
                if hasattr(self.matcher, "diagnostics"):
                    diagnostics.extend(list(self.matcher.diagnostics))

        combined: list[tuple[int, LLMMatch]] = []
        for index, matches in cached_matches:
            combined.extend((index, match) for match in matches)
        for index, matches in resolved_by_index.items():
            combined.extend((index, match) for match in matches)

        if diagnostics:
            self.diagnostics = diagnostics
        elif hasattr(self.matcher, "diagnostics"):
            self.diagnostics = list(self.matcher.diagnostics)
        self.stats = RelationCacheStats(
            reused_count=reused_count,
            resolved_count=len(misses),
            stored_count=len(misses),
        )
        ordered_matches = []
        for _index, match in sorted(
            combined,
            key=lambda item: (item[0], item[1].source_id, item[1].target_id),
        ):
            ordered_matches.append(match)
        return ordered_matches

    def manifest_metadata(self) -> dict[str, Any]:
        if hasattr(self.matcher, "manifest_metadata"):
            metadata = dict(self.matcher.manifest_metadata())
        else:
            metadata = {"matcher": self.matcher.__class__.__name__}
        metadata["relation_cache"] = {
            "schema_version": "Symphony-relation-match-cache-v1",
            "reused_count": self.stats.reused_count,
            "resolved_count": self.stats.resolved_count,
        }
        return metadata

    @property
    def thresholds(self) -> dict[str, float]:
        return dict(getattr(self.matcher, "thresholds", {}))


class RelationMatchCache:
    """JSON cache keyed by candidate evidence, fingerprints, and matcher config."""

    def __init__(
        self,
        path: str | Path,
        *,
        matcher_signature: dict[str, Any],
        fingerprints: Iterable[Fingerprint],
    ) -> None:
        self.path = Path(path).resolve()
        self.matcher_signature = matcher_signature
        self.fingerprint_hashes = {
            item.id: _stable_sha256(item.graph_identity_dict())
            for item in fingerprints
        }
        self._records = self._load()
        self._dirty = False

    def load(self, candidate: RelationCandidate) -> list[LLMMatch] | None:
        record = self._records.get(self._key(candidate))
        if not isinstance(record, dict):
            return None
        if record.get("schema_version") != "Symphony-relation-match-cache-v1":
            return None
        return [
            _match_from_dict(item)
            for item in record.get("matches", [])
            if isinstance(item, dict)
        ]

    def store(self, candidate: RelationCandidate, matches: list[LLMMatch]) -> None:
        self._records[self._key(candidate)] = {
            "schema_version": "Symphony-relation-match-cache-v1",
            "candidate_id": candidate.key,
            "matches": [match.to_dict() for match in matches],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self._dirty = True

    def flush(self) -> None:
        if not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": "Symphony-relation-match-cache-index-v1",
            "records": self._records,
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)
        self._dirty = False

    def _load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        records = payload.get("records")
        return records if isinstance(records, dict) else {}

    def _key(self, candidate: RelationCandidate) -> str:
        source_hash = self.fingerprint_hashes.get(candidate.source_id, "")
        target_hash = self.fingerprint_hashes.get(candidate.target_id, "")
        payload = {
            "candidate": candidate.to_dict(),
            "source_fingerprint_hash": source_hash,
            "target_fingerprint_hash": target_hash,
            "matcher": self.matcher_signature,
        }
        return _stable_sha256(payload)


def _matches_by_candidate(
    candidates: list[RelationCandidate],
    matches: list[LLMMatch],
) -> dict[str, list[LLMMatch]]:
    candidate_by_pair = {}
    for candidate in candidates:
        candidate_by_pair[(candidate.source_id, candidate.target_id)] = candidate.key
        candidate_by_pair[(candidate.target_id, candidate.source_id)] = candidate.key
    output: dict[str, list[LLMMatch]] = {candidate.key: [] for candidate in candidates}
    for match in matches:
        candidate_key = match.candidate_id or candidate_by_pair.get(
            (match.source_id, match.target_id)
        )
        if candidate_key in output:
            output[candidate_key].append(match)
    return output


def _matcher_signature(matcher: Any) -> dict[str, Any]:
    if hasattr(matcher, "manifest_metadata"):
        metadata = dict(matcher.manifest_metadata())
    else:
        metadata = {"matcher": matcher.__class__.__name__}
    metadata.pop("api_key", None)
    metadata.pop("base_url", None)
    thresholds = getattr(matcher, "thresholds", None)
    if thresholds is not None:
        metadata["thresholds"] = dict(thresholds)
    return metadata


def _matcher_batch_size(matcher: Any) -> int:
    try:
        return max(1, int(getattr(matcher, "batch_size", 0) or 0))
    except (TypeError, ValueError):
        return 1


def _matcher_max_workers(matcher: Any) -> int:
    try:
        return max(1, int(getattr(matcher, "max_workers", 0) or 0))
    except (TypeError, ValueError):
        return 1


def _matcher_window_size(matcher: Any) -> int:
    return _matcher_batch_size(matcher) * _matcher_max_workers(matcher)


def _chunked(values: list[Any], size: int) -> list[list[Any]]:
    return [
        values[index: index + size]
        for index in range(0, len(values), max(1, size))
    ]


def _match_from_dict(payload: dict[str, Any]) -> LLMMatch:
    return LLMMatch(
        source_id=str(payload.get("source_id") or ""),
        target_id=str(payload.get("target_id") or ""),
        relation_type=str(payload.get("relation_type") or ""),
        confidence=float(payload.get("confidence") or 0.0),
        method=str(payload.get("method") or "llm_ontology_match"),
        reasons=[str(item) for item in payload.get("reasons", [])],
        supporting_fields=(
            payload.get("supporting_fields")
            if isinstance(payload.get("supporting_fields"), dict)
            else {}
        ),
        candidate_id=payload.get("candidate_id"),
        accepted=bool(payload.get("accepted", False)),
        diagnostics=[str(item) for item in payload.get("diagnostics", [])],
        raw=payload.get("raw") if isinstance(payload.get("raw"), dict) else {},
    )


def _stable_sha256(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
